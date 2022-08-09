"""Core code for adversarial imitation learning, shared between GAIL and AIRL."""
import abc
import collections
import dataclasses
import logging
import os
from typing import Callable, Mapping, Optional, Sequence, Tuple, Type, cast

import numpy as np
import torch as th
import torch.utils.tensorboard as thboard
import tqdm
from stable_baselines3.common import base_class, policies, vec_env
from stable_baselines3.sac import policies as sac_policies
from torch.nn import functional as F

from imitation.algorithms import base
from imitation.data import buffer, rollout, types, wrappers
from imitation.rewards import reward_nets, reward_wrapper
from imitation.util import logger, networks, util

import omegaconf
import mbrl.env.reward_fns as reward_fns
import mbrl.env.termination_fns as termination_fns
import mbrl.models as models
import mbrl.planning as planning
import mbrl.util.common as common_util

import mbrl.util as mbrl_util
from mbrl import constants
import mbrl.types as mbrl_types




def compute_train_stats(
    disc_logits_expert_is_high: th.Tensor,
    labels_expert_is_one: th.Tensor,
    disc_loss: th.Tensor,
) -> Mapping[str, float]:
    """Train statistics for GAIL/AIRL discriminator.

    Args:
        disc_logits_expert_is_high: discriminator logits produced by
            `AdversarialTrainer.logits_expert_is_high`.
        labels_expert_is_one: integer labels describing whether logit was for an
            expert (0) or generator (1) sample.
        disc_loss: final discriminator loss.

    Returns:
        A mapping from statistic names to float values.
    """
    with th.no_grad():
        # Logits of the discriminator output; >0 for expert samples, <0 for generator.
        bin_is_generated_pred = disc_logits_expert_is_high < 0
        # Binary label, so 1 is for expert, 0 is for generator.
        bin_is_generated_true = labels_expert_is_one == 0
        bin_is_expert_true = th.logical_not(bin_is_generated_true)
        int_is_generated_pred = bin_is_generated_pred.long()
        int_is_generated_true = bin_is_generated_true.long()
        n_generated = float(th.sum(int_is_generated_true))
        n_labels = float(len(labels_expert_is_one))
        n_expert = n_labels - n_generated
        pct_expert = n_expert / float(n_labels) if n_labels > 0 else float("NaN")
        n_expert_pred = int(n_labels - th.sum(int_is_generated_pred))
        if n_labels > 0:
            pct_expert_pred = n_expert_pred / float(n_labels)
        else:
            pct_expert_pred = float("NaN")
        correct_vec = th.eq(bin_is_generated_pred, bin_is_generated_true)
        acc = th.mean(correct_vec.float())

        _n_pred_expert = th.sum(th.logical_and(bin_is_expert_true, correct_vec))
        if n_expert < 1:
            expert_acc = float("NaN")
        else:
            # float() is defensive, since we cannot divide Torch tensors by
            # Python ints
            expert_acc = _n_pred_expert / float(n_expert)

        _n_pred_gen = th.sum(th.logical_and(bin_is_generated_true, correct_vec))
        _n_gen_or_1 = max(1, n_generated)
        generated_acc = _n_pred_gen / float(_n_gen_or_1)

        label_dist = th.distributions.Bernoulli(logits=disc_logits_expert_is_high)
        entropy = th.mean(label_dist.entropy())

    pairs = [
        ("disc_loss", float(th.mean(disc_loss))),
        # accuracy, as well as accuracy on *just* expert examples and *just*
        # generated examples
        ("disc_acc", float(acc)),
        ("disc_acc_expert", float(expert_acc)),
        ("disc_acc_gen", float(generated_acc)),
        # entropy of the predicted label distribution, averaged equally across
        # both classes (if this drops then disc is very good or has given up)
        ("disc_entropy", float(entropy)),
        # true number of expert demos and predicted number of expert demos
        ("disc_proportion_expert_true", float(pct_expert)),
        ("disc_proportion_expert_pred", float(pct_expert_pred)),
        ("n_expert", float(n_expert)),
        ("n_generated", float(n_generated)),
    ]  # type: Sequence[Tuple[str, float]]
    return collections.OrderedDict(pairs)


class AdversarialTrainer(base.DemonstrationAlgorithm[types.Transitions]):
    """Base class for adversarial imitation learning algorithms like GAIL and AIRL."""

    venv: vec_env.VecEnv
    """The original vectorized environment."""

    venv_train: vec_env.VecEnv
    """Like `self.venv`, but wrapped with train reward unless in debug mode.

    If `debug_use_ground_truth=True` was passed into the initializer then
    `self.venv_train` is the same as `self.venv`."""

    def __init__(
        self,
        *,
        demonstrations: base.AnyTransitions,
        demo_batch_size: int,
        venv: vec_env.VecEnv,
#         gen_algo: base_class.BaseAlgorithm,
        reward_net: reward_nets.RewardNet,
        n_disc_updates_per_round: int = 2,
        log_dir: str = "output/",
        disc_opt_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        disc_opt_kwargs: Optional[Mapping] = None,
        gen_train_timesteps: Optional[int] = None,
        gen_replay_buffer_capacity: Optional[int] = None,
        custom_logger: Optional[logger.HierarchicalLogger] = None,
        init_tensorboard: bool = False,
        init_tensorboard_graph: bool = False,
        debug_use_ground_truth: bool = False,
        allow_variable_horizon: bool = False,
        
        gen_algo,        
        dynamics_model,
        cfg,
        replay_buffer,
        model_trainer,
        term_fn,
        torch_generator
        
    ):
        """Builds AdversarialTrainer.

        Args:
            demonstrations: Demonstrations from an expert (optional). Transitions
                expressed directly as a `types.TransitionsMinimal` object, a sequence
                of trajectories, or an iterable of transition batches (mappings from
                keywords to arrays containing observations, etc).
            demo_batch_size: The number of samples in each batch of expert data. The
                discriminator batch size is twice this number because each discriminator
                batch contains a generator sample for every expert sample.
            venv: The vectorized environment to train in.
            gen_algo: The generator RL algorithm that is trained to maximize
                discriminator confusion. Environment and logger will be set to
                `venv` and `custom_logger`.
            reward_net: a Torch module that takes an observation, action and
                next observation tensors as input and computes a reward signal.
            n_disc_updates_per_round: The number of discriminator updates after each
                round of generator updates in AdversarialTrainer.learn().
            log_dir: Directory to store TensorBoard logs, plots, etc. in.
            disc_opt_cls: The optimizer for discriminator training.
            disc_opt_kwargs: Parameters for discriminator training.
            gen_train_timesteps: The number of steps to train the generator policy for
                each iteration. If None, then defaults to the batch size (for on-policy)
                or number of environments (for off-policy).
            gen_replay_buffer_capacity: The capacity of the
                generator replay buffer (the number of obs-action-obs samples from
                the generator that can be stored). By default this is equal to
                `gen_train_timesteps`, meaning that we sample only from the most
                recent batch of generator samples.
            custom_logger: Where to log to; if None (default), creates a new logger.
            init_tensorboard: If True, makes various discriminator
                TensorBoard summaries.
            init_tensorboard_graph: If both this and `init_tensorboard` are True,
                then write a Tensorboard graph summary to disk.
            debug_use_ground_truth: If True, use the ground truth reward for
                `self.train_env`.
                This disables the reward wrapping that would normally replace
                the environment reward with the learned reward. This is useful for
                sanity checking that the policy training is functional.
            allow_variable_horizon: If False (default), algorithm will raise an
                exception if it detects trajectories of different length during
                training. If True, overrides this safety check. WARNING: variable
                horizon episodes leak information about the reward via termination
                condition, and can seriously confound evaluation. Read
                https://imitation.readthedocs.io/en/latest/guide/variable_horizon.html
                before overriding this.
        """
        self.demo_batch_size = demo_batch_size
        self._demo_data_loader = None
        self._endless_expert_iterator = None
        super().__init__(
            demonstrations=demonstrations,
            custom_logger=custom_logger,
            allow_variable_horizon=allow_variable_horizon,
        )

        self._global_step = 0
        self._disc_step = 0
        self.n_disc_updates_per_round = n_disc_updates_per_round

        self.debug_use_ground_truth = debug_use_ground_truth
        self.venv = venv
        self.gen_algo = gen_algo
        
        self._reward_net = reward_net
#         self._reward_net = reward_net.to(gen_algo.device) ---------------------------------
        self._log_dir = log_dir
    

        # Create graph for optimising/recording stats on discriminator
        self._disc_opt_cls = disc_opt_cls
        self._disc_opt_kwargs = disc_opt_kwargs or {}
        self._init_tensorboard = init_tensorboard
        self._init_tensorboard_graph = init_tensorboard_graph
        self._disc_opt = self._disc_opt_cls(
            self._reward_net.parameters(),
            **self._disc_opt_kwargs,
        )

        if self._init_tensorboard:
            logging.info("building summary directory at " + self._log_dir)
            summary_dir = os.path.join(self._log_dir, "summary")
            os.makedirs(summary_dir, exist_ok=True)
            self._summary_writer = thboard.SummaryWriter(summary_dir)

        venv = self.venv_buffering = wrappers.BufferingWrapper(self.venv)

        if debug_use_ground_truth:
            # Would use an identity reward fn here, but RewardFns can't see rewards.
            self.venv_wrapped = venv
            self.gen_callback = None
        else:
            venv = self.venv_wrapped = reward_wrapper.RewardVecEnvWrapper(
                venv,
                reward_fn=self.reward_train.predict_processed,
            )
            self.gen_callback = self.venv_wrapped.make_log_callback()
        self.venv_train = self.venv_wrapped

#######################################################################
        # In train_gen() the step is performed on self.venv_train
        # self.gen_algo.set_env(self.venv_train) 


        # 'TrajectoryOptimizerAgent' object has no attribute 'set_logger' 
        # self.gen_algo.set_logger(self.logger)       


        # 'TrajectoryOptimizerAgent' object has no attribute 'get_env'

        # if gen_train_timesteps is None:
        #     gen_algo_env = self.gen_algo.get_env()
        #     assert gen_algo_env is not None
        #     self.gen_train_timesteps = gen_algo_env.num_envs
        #     if hasattr(self.gen_algo, "n_steps"):  # on policy
        #         self.gen_train_timesteps *= self.gen_algo.n_steps
        # else:
        #     self.gen_train_timesteps = gen_train_timesteps
        
        self.gen_train_timesteps = gen_train_timesteps
#######################################################################


        if gen_replay_buffer_capacity is None:
            gen_replay_buffer_capacity = self.gen_train_timesteps
        self._gen_replay_buffer = buffer.ReplayBuffer(
            gen_replay_buffer_capacity,
            self.venv,
        )
        ###################### new variables #############################
        self.dynamics_model = dynamics_model
        self.cfg = cfg
        self.replay_buffer = replay_buffer
        self.model_trainer = model_trainer
        self.ensemble_size = 1
        self.term_fn = term_fn
        self.torch_generator= torch_generator
        self.work_dir = os.getcwd()

    @property
    def policy(self) -> policies.BasePolicy:
        return self.gen_algo.policy

    @abc.abstractmethod
    def logits_expert_is_high(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
        log_policy_act_prob: Optional[th.Tensor] = None,
    ) -> th.Tensor:
        """Compute the discriminator's logits for each state-action sample.

        A high value corresponds to predicting expert, and a low value corresponds to
        predicting generator.

        Args:
            state: state at time t, of shape `(batch_size,) + state_shape`.
            action: action taken at time t, of shape `(batch_size,) + action_shape`.
            next_state: state at time t+1, of shape `(batch_size,) + state_shape`.
            done: binary episode completion flag after action at time t,
                of shape `(batch_size,)`.
            log_policy_act_prob: log probability of generator policy taking
                `action` at time t.

        Returns:
            Discriminator logits of shape `(batch_size,)`. A high output indicates an
            expert-like transition.
        """  # noqa: DAR202

    @property
    @abc.abstractmethod
    def reward_train(self) -> reward_nets.RewardNet:
        """Reward used to train generator policy."""

    @property
    @abc.abstractmethod
    def reward_test(self) -> reward_nets.RewardNet:
        """Reward used to train policy at "test" time after adversarial training."""

    def set_demonstrations(self, demonstrations: base.AnyTransitions) -> None:
        self._demo_data_loader = base.make_data_loader(
            demonstrations,
            self.demo_batch_size,
        )
        self._endless_expert_iterator = util.endless_iter(self._demo_data_loader)

    def _next_expert_batch(self) -> Mapping:
        return next(self._endless_expert_iterator)

    def train_disc(
        self,
        *,
        expert_samples: Optional[Mapping] = None,
        gen_samples: Optional[Mapping] = None,
    ) -> Optional[Mapping[str, float]]:
        """Perform a single discriminator update, optionally using provided samples.

        Args:
            expert_samples: Transition samples from the expert in dictionary form.
                If provided, must contain keys corresponding to every field of the
                `Transitions` dataclass except "infos". All corresponding values can be
                either NumPy arrays or Tensors. Extra keys are ignored. Must contain
                `self.demo_batch_size` samples. If this argument is not provided, then
                `self.demo_batch_size` expert samples from `self.demo_data_loader` are
                used by default.
            gen_samples: Transition samples from the generator policy in same dictionary
                form as `expert_samples`. If provided, must contain exactly
                `self.demo_batch_size` samples. If not provided, then take
                `len(expert_samples)` samples from the generator replay buffer.

        Returns:
            Statistics for discriminator (e.g. loss, accuracy).
        """
        with self.logger.accumulate_means("disc"):
            # optionally write TB summaries for collected ops
            write_summaries = self._init_tensorboard and self._global_step % 20 == 0

            # compute loss
            batch = self._make_disc_train_batch(
                gen_samples=gen_samples,
                expert_samples=expert_samples,
            )
            disc_logits = self.logits_expert_is_high(
                batch["state"],
                batch["action"],
                batch["next_state"],
                batch["done"],
                batch["log_policy_act_prob"],
            )
            loss = F.binary_cross_entropy_with_logits(
                disc_logits,
                batch["labels_expert_is_one"].float(),
            )

            # do gradient step
            self._disc_opt.zero_grad()
            loss.backward()
            self._disc_opt.step()
            self._disc_step += 1

            # compute/write stats and TensorBoard data
            with th.no_grad():
                train_stats = compute_train_stats(
                    disc_logits,
                    batch["labels_expert_is_one"],
                    loss,
                )
            self.logger.record("global_step", self._global_step)
            for k, v in train_stats.items():
                self.logger.record(k, v)
            self.logger.dump(self._disc_step)
            if write_summaries:
                self._summary_writer.add_histogram("disc_logits", disc_logits.detach())

        return train_stats
#################################################################################################
    def rollout_model_and_populate_sac_buffer(self,
        model_env: models.ModelEnv,
        replay_buffer: mbrl_util.ReplayBuffer,
        agent,
        sac_buffer: mbrl_util.ReplayBuffer,
        sac_samples_action: bool,
        rollout_horizon: int,
        batch_size: int,
    ):

        batch = replay_buffer.sample(batch_size)
        initial_obs, *_ = cast(mbrl_types.TransitionBatch, batch).astuple()
        model_state = model_env.reset(
            initial_obs_batch=cast(np.ndarray, initial_obs),
            return_as_np=True,
        )
        accum_dones = np.zeros(initial_obs.shape[0], dtype=bool)
        obs = initial_obs
        for i in range(rollout_horizon):
            action = agent.act(obs, sample=sac_samples_action, batched=True)
            pred_next_obs, pred_rewards, pred_dones, model_state = model_env.step(
                action, model_state, sample=True
            )
            sac_buffer.add_batch(
                obs[~accum_dones],
                action[~accum_dones],
                pred_next_obs[~accum_dones],
                pred_rewards[~accum_dones, 0],
                pred_dones[~accum_dones, 0],
            )
            obs = pred_next_obs
            accum_dones |= pred_dones.squeeze()


    def evaluate(self,
        env,
        agent,
        num_episodes: int,
        video_recorder,
    ) -> float:
        avg_episode_reward = 0
        for episode in range(num_episodes):
            obs = env.reset()
            video_recorder.init(enabled=(episode == 0))
            done = False
            episode_reward = 0
            while not done:
                action = agent.act(obs)
                obs, reward, done, _ = env.step(action)
                video_recorder.record(env)
                episode_reward += reward
            avg_episode_reward += episode_reward
        return avg_episode_reward / num_episodes


    def maybe_replace_sac_buffer(self,
        sac_buffer: Optional[mbrl_util.ReplayBuffer],
        obs_shape: Sequence[int],
        act_shape: Sequence[int],
        new_capacity: int,
        seed: int,
    ) -> mbrl_util.ReplayBuffer:
        if sac_buffer is None or new_capacity != sac_buffer.capacity:
            if sac_buffer is None:
                rng = np.random.default_rng(seed=seed)
            else:
                rng = sac_buffer.rng
            new_buffer = mbrl_util.ReplayBuffer(new_capacity, obs_shape, act_shape, rng=rng)
            if sac_buffer is None:
                return new_buffer
            obs, action, next_obs, reward, done = sac_buffer.get_all().astuple()
            new_buffer.add_batch(obs, action, next_obs, reward, done)
            return new_buffer
        return sac_buffer

#################################################################################################
    def train_gen(
        self,
        total_timesteps: Optional[int] = None,
        learn_kwargs: Optional[Mapping] = None,
    ) -> None:
        """Trains the generator to maximize the discriminator loss.

        After the end of training populates the generator replay buffer (used in
        discriminator training) with `self.disc_batch_size` transitions.

        Args:
            total_timesteps: The number of transitions to sample from
                `self.venv_train` during training. By default,
                `self.gen_train_timesteps`.
            learn_kwargs: kwargs for the Stable Baselines `RLModel.learn()`
                method.
        """
        if total_timesteps is None:
            total_timesteps = self.gen_train_timesteps
        if learn_kwargs is None:
            learn_kwargs = {}

            def rollout_model_and_populate_sac_buffer(
                model_env: models.ModelEnv,
                replay_buffer: mbrl_util.ReplayBuffer,
                agent,
                sac_buffer: mbrl_util.ReplayBuffer,
                sac_samples_action: bool,
                rollout_horizon: int,
                batch_size: int,
            ):

                batch = replay_buffer.sample(batch_size)
                initial_obs, *_ = cast(mbrl_types.TransitionBatch, batch).astuple()
                model_state = model_env.reset(
                    initial_obs_batch=cast(np.ndarray, initial_obs),
                    return_as_np=True,
                )
                accum_dones = np.zeros(initial_obs.shape[0], dtype=bool)
                obs = initial_obs
                for i in range(rollout_horizon):
                    action = agent.act(obs, sample=sac_samples_action, batched=True)
                    pred_next_obs, pred_rewards, pred_dones, model_state = model_env.step(
                        action, model_state, sample=True
                    )
                    sac_buffer.add_batch(
                        obs[~accum_dones],
                        action[~accum_dones],
                        pred_next_obs[~accum_dones],
                        pred_rewards[~accum_dones, 0],
                        pred_dones[~accum_dones, 0],
                    )
                    obs = pred_next_obs
                    accum_dones |= pred_dones.squeeze()


            def evaluate(
                env,
                agent,
                num_episodes: int,
                video_recorder,
            ) -> float:
                avg_episode_reward = 0
                for episode in range(num_episodes):
                    obs = env.reset()
                    # video_recorder.init(enabled=(episode == 0))
                    done = False
                    episode_reward = 0
                    while not done:
                        action = agent.act(obs)
                        obs, reward, done, _ = env.step(action)
                        # video_recorder.record(env)
                        episode_reward += reward
                    avg_episode_reward += episode_reward
                return avg_episode_reward / num_episodes


            def maybe_replace_sac_buffer(
                sac_buffer: Optional[mbrl_util.ReplayBuffer],
                obs_shape: Sequence[int],
                act_shape: Sequence[int],
                new_capacity: int,
                seed: int,
            ) -> mbrl_util.ReplayBuffer:
                if sac_buffer is None or new_capacity != sac_buffer.capacity:
                    if sac_buffer is None:
                        rng = np.random.default_rng(seed=seed)
                    else:
                        rng = sac_buffer.rng
                    new_buffer = mbrl_util.ReplayBuffer(new_capacity, obs_shape, act_shape, rng=rng)
                    if sac_buffer is None:
                        return new_buffer
                    obs, action, next_obs, reward, done = sac_buffer.get_all().astuple()
                    new_buffer.add_batch(obs, action, next_obs, reward, done)
                    return new_buffer
                return sac_buffer
            
 ######################## CHANGE FREE MODEL BASED TO MODEL BASED ########################################################
#         with self.logger.accumulate_means("gen"):
#             self.gen_algo.learn(
#                 total_timesteps=total_timesteps,
#                 reset_num_timesteps=False,
#                 callback=self.gen_callback,
#                 **learn_kwargs,
#             )
#             self._global_step += 1

        silent = True
        test_env = self.venv_train
        video_recorder = None
        rng = np.random.default_rng(seed=self.cfg.seed)
        logger = None

        # --------------------- Training Loop ---------------------
        rollout_batch_size = (
            self.cfg.overrides.effective_model_rollouts_per_step * self.cfg.algorithm.freq_train_model
        )
        trains_per_epoch = int(
            np.ceil(self.cfg.overrides.epoch_length / self.cfg.overrides.freq_train_model)
        )
        updates_made = 0
        env_steps = 0
        model_env = models.ModelEnv(
            self.venv_train, self.dynamics_model, self.term_fn, None, generator=self.torch_generator
        )
        model_trainer = models.ModelTrainer(
            self.dynamics_model,
            optim_lr=self.cfg.overrides.model_lr,
            weight_decay=self.cfg.overrides.model_wd,
            logger=None if silent else logger,
        )
        best_eval_reward = -np.inf
        epoch = 0
        sac_buffer = None
        while env_steps < self.cfg.overrides.num_steps:
            rollout_length = int(
                mbrl_util.math.truncated_linear( #util.math
                    *(self.cfg.overrides.rollout_schedule + [epoch + 1])
                )
            )
            sac_buffer_capacity = rollout_length * rollout_batch_size * trains_per_epoch
            sac_buffer_capacity *= self.cfg.overrides.num_epochs_to_retain_sac_buffer
            sac_buffer = maybe_replace_sac_buffer(
                sac_buffer, self.venv_train.observation_space.shape , self.venv_train.action_space.shape, sac_buffer_capacity, seed=self.cfg.seed
            )
            obs, done = None, False
            for steps_epoch in range(self.cfg.overrides.epoch_length):
                if steps_epoch == 0 or done:
                    obs, done = self.venv_train.reset(), False
                # --- Doing env step and adding to model dataset ---
                next_obs, reward, done, _ = mbrl_util.common.step_env_and_add_to_buffer(
                    self.venv_train, obs, self.gen_algo, {}, self.replay_buffer
                )

                # --------------- Model Training -----------------
                if (env_steps + 1) % self.cfg.overrides.freq_train_model == 0:
                    mbrl_util.common.train_model_and_save_model_and_data(
                        self.dynamics_model,
                        model_trainer,
                        self.cfg.overrides,
                        self.replay_buffer,
                        work_dir=self.work_dir,
                    )

                    # --------- Rollout new model and store imagined trajectories --------
                    # Batch all rollouts for the next freq_train_model steps together
                    rollout_model_and_populate_sac_buffer(
                        model_env,
                        self.replay_buffer,
                        self.gen_algo,
                        sac_buffer,
                        self.cfg.algorithm.sac_samples_action,
                        rollout_length,
                        rollout_batch_size,
                    )

                    debug_mode = False ###########################
                    if debug_mode:
                        print(
                            f"Epoch: {epoch}. "
                            f"SAC buffer size: {len(sac_buffer)}. "
                            f"Rollout length: {rollout_length}. "
                            f"Steps: {env_steps}"
                        )

                # --------------- Agent Training -----------------
                for _ in range(self.cfg.overrides.num_sac_updates_per_step):
                    use_real_data = rng.random() < self.cfg.algorithm.real_data_ratio
                    which_buffer = self.replay_buffer if use_real_data else sac_buffer
                    if (env_steps + 1) % self.cfg.overrides.sac_updates_every_steps != 0 or len(
                        which_buffer
                    ) < self.cfg.overrides.sac_batch_size:
                        break  # only update every once in a while

                    self.gen_algo.sac_agent.update_parameters(
                        which_buffer,
                        self.cfg.overrides.sac_batch_size,
                        updates_made,
                        logger,
                        reverse_mask=True,
                    )
                    updates_made += 1
                    # if not silent and updates_made % cfg.log_frequency_agent == 0:
                    #     logger.dump(updates_made, save=True)

                # ------ Epoch ended (evaluate and save model) ------
                if (env_steps + 1) % self.cfg.overrides.epoch_length == 0:
                    avg_reward = evaluate(
                        test_env, self.gen_algo, self.cfg.algorithm.num_eval_episodes, video_recorder
                    )
                    # logger.log_data(
                    #     constants.RESULTS_LOG_NAME,
                    #     {
                    #         "epoch": epoch,
                    #         "env_step": env_steps,
                    #         "episode_reward": avg_reward,
                    #         "rollout_length": rollout_length,
                    #     },
                    # )
                    if avg_reward > best_eval_reward:
                        # video_recorder.save(f"{epoch}.mp4")
                        best_eval_reward = avg_reward
                        self.gen_algo.sac_agent.save_checkpoint(
                            ckpt_path=os.path.join(self.work_dir, "sac.pth")
                        )
                    epoch += 1

                env_steps += 1
                obs = next_obs


 ######################## ###################################### ########################################################

        gen_trajs, ep_lens = self.venv_buffering.pop_trajectories()
        self._check_fixed_horizon(ep_lens)
        gen_samples = rollout.flatten_trajectories_with_rew(gen_trajs)
        self._gen_replay_buffer.store(gen_samples)

    def train(
        self,
        total_timesteps: int,
        callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Alternates between training the generator and discriminator.

        Every "round" consists of a call to `train_gen(self.gen_train_timesteps)`,
        a call to `train_disc`, and finally a call to `callback(round)`.

        Training ends once an additional "round" would cause the number of transitions
        sampled from the environment to exceed `total_timesteps`.

        Args:
            total_timesteps: An upper bound on the number of transitions to sample
                from the environment during training.
            callback: A function called at the end of every round which takes in a
                single argument, the round number. Round numbers are in
                `range(total_timesteps // self.gen_train_timesteps)`.
        """
        n_rounds = total_timesteps // self.gen_train_timesteps
        assert n_rounds >= 1, (
            "No updates (need at least "
            f"{self.gen_train_timesteps} timesteps, have only "
            f"total_timesteps={total_timesteps})!"
        )
        for r in tqdm.tqdm(range(0, n_rounds), desc="round"):
            self.train_gen(self.gen_train_timesteps)
            for _ in range(self.n_disc_updates_per_round):
                with networks.training(self.reward_train):
                    # switch to training mode (affects dropout, normalization)
                    self.train_disc()
            if callback:
                callback(r)
            self.logger.dump(self._global_step)

    def _torchify_array(self, ndarray: Optional[np.ndarray]) -> Optional[th.Tensor]:
        if ndarray is not None:
            return th.as_tensor(ndarray, device=self.reward_train.device)

    def _get_log_policy_act_prob(
        self,
        obs_th: th.Tensor,
        acts_th: th.Tensor,
    ) -> Optional[th.Tensor]:
        """Evaluates the given actions on the given observations.

        Args:
            obs_th: A batch of observations.
            acts_th: A batch of actions.

        Returns:
            A batch of log policy action probabilities.
        """
        if isinstance(self.policy, policies.ActorCriticPolicy):
            # policies.ActorCriticPolicy has a concrete implementation of
            # evaluate_actions to generate log_policy_act_prob given obs and actions.
            _, log_policy_act_prob_th, _ = self.policy.evaluate_actions(
                obs_th,
                acts_th,
            )
        elif isinstance(self.policy, sac_policies.SACPolicy):
            gen_algo_actor = self.policy.actor
            assert gen_algo_actor is not None
            # generate log_policy_act_prob from SAC actor.
            mean_actions, log_std, _ = gen_algo_actor.get_action_dist_params(obs_th)
            distribution = gen_algo_actor.action_dist.proba_distribution(
                mean_actions,
                log_std,
            )
            # SAC applies a squashing function to bound the actions to a finite range
            # `acts_th` need to be scaled accordingly before computing log prob.
            # Scale actions only if the policy squashes outputs.
            assert self.policy.squash_output
            scaled_acts_th = self.policy.scale_action(acts_th)
            log_policy_act_prob_th = distribution.log_prob(scaled_acts_th)
        else:
            return None
        return log_policy_act_prob_th

    def _make_disc_train_batch(
        self,
        *,
        gen_samples: Optional[Mapping] = None,
        expert_samples: Optional[Mapping] = None,
    ) -> Mapping[str, th.Tensor]:
        """Build and return training batch for the next discriminator update.

        Args:
            gen_samples: Same as in `train_disc`.
            expert_samples: Same as in `train_disc`.

        Returns:
            The training batch: state, action, next state, dones, labels
            and policy log-probabilities.

        Raises:
            RuntimeError: Empty generator replay buffer.
            ValueError: `gen_samples` or `expert_samples` batch size is
                different from `self.demo_batch_size`.
        """
        if expert_samples is None:
            expert_samples = self._next_expert_batch()

        if gen_samples is None:
            if self._gen_replay_buffer.size() == 0:
                raise RuntimeError(
                    "No generator samples for training. " "Call `train_gen()` first.",
                )
            gen_samples = self._gen_replay_buffer.sample(self.demo_batch_size)
            gen_samples = types.dataclass_quick_asdict(gen_samples)

        n_gen = len(gen_samples["obs"])
        n_expert = len(expert_samples["obs"])
        if not (n_gen == n_expert == self.demo_batch_size):
            raise ValueError(
                "Need to have exactly self.demo_batch_size number of expert and "
                "generator samples, each. "
                f"(n_gen={n_gen} n_expert={n_expert} "
                f"demo_batch_size={self.demo_batch_size})",
            )

        # Guarantee that Mapping arguments are in mutable form.
        expert_samples = dict(expert_samples)
        gen_samples = dict(gen_samples)

        # Convert applicable Tensor values to NumPy.
        for field in dataclasses.fields(types.Transitions):
            k = field.name
            if k == "infos":
                continue
            for d in [gen_samples, expert_samples]:
                if isinstance(d[k], th.Tensor):
                    d[k] = d[k].detach().numpy()
        assert isinstance(gen_samples["obs"], np.ndarray)
        assert isinstance(expert_samples["obs"], np.ndarray)

        # Check dimensions.
        n_samples = n_expert + n_gen
        assert n_expert == len(expert_samples["acts"])
        assert n_expert == len(expert_samples["next_obs"])
        assert n_gen == len(gen_samples["acts"])
        assert n_gen == len(gen_samples["next_obs"])

        # Concatenate rollouts, and label each row as expert or generator.
        obs = np.concatenate([expert_samples["obs"], gen_samples["obs"]])
        acts = np.concatenate([expert_samples["acts"], gen_samples["acts"]])
        next_obs = np.concatenate([expert_samples["next_obs"], gen_samples["next_obs"]])
        dones = np.concatenate([expert_samples["dones"], gen_samples["dones"]])
        # notice that the labels use the convention that expert samples are
        # labelled with 1 and generator samples with 0.
        labels_expert_is_one = np.concatenate(
            [np.ones(n_expert, dtype=int), np.zeros(n_gen, dtype=int)],
        )

        # Calculate generator-policy log probabilities.
        with th.no_grad():
            obs_th = th.as_tensor(obs, device="cpu") #from typing import Optional, Sequence, cast
            acts_th = th.as_tensor(acts, device="cpu")
            log_policy_act_prob = self._get_log_policy_act_prob(obs_th, acts_th)
            if log_policy_act_prob is not None:
                assert len(log_policy_act_prob) == n_samples
                log_policy_act_prob = log_policy_act_prob.reshape((n_samples,))
            del obs_th, acts_th  # unneeded

        obs_th, acts_th, next_obs_th, dones_th = self.reward_train.preprocess(
            obs,
            acts,
            next_obs,
            dones,
        )
        batch_dict = {
            "state": obs_th,
            "action": acts_th,
            "next_state": next_obs_th,
            "done": dones_th,
            "labels_expert_is_one": self._torchify_array(labels_expert_is_one),
            "log_policy_act_prob": log_policy_act_prob,
        }

        return batch_dict
