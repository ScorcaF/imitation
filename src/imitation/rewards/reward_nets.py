"""Constructs deep network reward models."""

import abc
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple, Type

import gym
import numpy as np
import torch as th
from stable_baselines3.common import preprocessing
from torch import nn

from imitation.util import networks, util


class RewardNet(nn.Module, abc.ABC):
    """Minimal abstract reward network.

    Only requires the implementation of a forward pass (calculating rewards given
    a batch of states, actions, next states and dones).
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        normalize_images: bool = True,
    ):
        """Initialize the RewardNet.

        Args:
            observation_space: the observation space of the environment
            action_space: the action space of the environment
            normalize_images: whether to automatically normalize
                image observations to [0, 1] (from 0 to 255). Defaults to True.
        """
        super().__init__()
        self.observation_space = observation_space
        self.action_space = action_space
        self.normalize_images = normalize_images

    @abc.abstractmethod
    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ) -> th.Tensor:
        """Compute rewards for a batch of transitions and keep gradients."""

    def preprocess(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> Tuple[th.Tensor, th.Tensor, th.Tensor, th.Tensor]:
        """Preprocess a batch of input transitions and convert it to PyTorch tensors.

        The output of this function is suitable for its forward pass,
        so a typical usage would be ``model(*model.preprocess(transitions))``.

        Args:
            state: The observation input. Its shape is
                `(batch_size,) + observation_space.shape`.
            action: The action input. Its shape is
                `(batch_size,) + action_space.shape`. The None dimension is
                expected to be the same as None dimension from `obs_input`.
            next_state: The observation input. Its shape is
                `(batch_size,) + observation_space.shape`.
            done: Whether the episode has terminated. Its shape is `(batch_size,)`.

        Returns:
            Preprocessed transitions: a Tuple of tensors containing
            observations, actions, next observations and dones.
        """
        state_th = util.safe_to_tensor(state).to(self.device)
        action_th = util.safe_to_tensor(action).to(self.device)
        next_state_th = util.safe_to_tensor(next_state).to(self.device)
        done_th = util.safe_to_tensor(done).to(self.device)

        del state, action, next_state, done  # unused

        # preprocess
        state_th = preprocessing.preprocess_obs(
            state_th,
            self.observation_space,
            self.normalize_images,
        )
        action_th = preprocessing.preprocess_obs(
            action_th,
            self.action_space,
            self.normalize_images,
        )
        next_state_th = preprocessing.preprocess_obs(
            next_state_th,
            self.observation_space,
            self.normalize_images,
        )
        done_th = done_th.to(th.float32)

        n_gen = len(state_th)
        assert state_th.shape == next_state_th.shape
        assert len(action_th) == n_gen

        return state_th, action_th, next_state_th, done_th

    def predict_th(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> th.Tensor:
        """Compute th.Tensor rewards for a batch of transitions without gradients.

        Preprocesses the inputs, output th.Tensor reward arrays.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed th.Tensor rewards of shape `(batch_size,`).
        """
        with networks.evaluating(self):
            # switch to eval mode (affecting normalization, dropout, etc)

            state_th, action_th, next_state_th, done_th = self.preprocess(
                state,
                action,
                next_state,
                done,
            )
            with th.no_grad():
                rew_th = self(state_th, action_th, next_state_th, done_th)

            assert rew_th.shape == state.shape[:1]
            return rew_th

    def predict(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> np.ndarray:
        """Compute rewards for a batch of transitions without gradients.

        Converting th.Tensor rewards from `predict_th` to NumPy arrays.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed rewards of shape `(batch_size,`).
        """
        rew_th = self.predict_th(state, action, next_state, done)
        return rew_th.detach().cpu().numpy().flatten()

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
        **kwargs,
    ) -> np.ndarray:
        """Compute the processed rewards for a batch of transitions without gradients.

        Defaults to calling `predict`. Subclasses can override this to normalize or
        otherwise modify the rewards in ways that may help RL training or other
        applications of the reward function.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.
            kwargs: additional kwargs may be passed to change the functionality of
                subclasses.

        Returns:
            Computed processed rewards of shape `(batch_size,`).
        """
        del kwargs
        return self.predict(state, action, next_state, done)

    @property
    def device(self) -> th.device:
        """Heuristic to determine which device this module is on."""
        try:
            first_param = next(self.parameters())
            return first_param.device
        except StopIteration:
            # if the model has no parameters, we use the CPU
            return th.device("cpu")

    @property
    def dtype(self) -> th.dtype:
        """Heuristic to determine dtype of module."""
        try:
            first_param = next(self.parameters())
            return first_param.dtype
        except StopIteration:
            # if the model has no parameters, default to float32
            return th.get_default_dtype()


class RewardNetWrapper(RewardNet):
    """An abstract RewardNet wrapping a base network.

    A concrete implementation of the `forward` method is needed.
    Note: by default, `predict`, `predict_th`, `preprocess`,
    `device` and all the PyTorch `nn.Module` methods will be inherited from `RewardNet`
    and not passed through to the base network. If any of these methods is overridden
    in the base `RewardNet`, this will not affect `RewardNetWrapper`. However,
    `predict_processed` is overridden and will call predict processed on the base
    net passing along all kwargs.
    """

    def __init__(
        self,
        base: RewardNet,
    ):
        """Initialize a RewardNet wrapper.

        Args:
            base: the base RewardNet to wrap.
        """
        super().__init__(
            base.observation_space,
            base.action_space,
            base.normalize_images,
        )
        self._base = base

    @property
    def base(self) -> RewardNet:
        return self._base

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
        **kwargs,
    ) -> np.ndarray:
        __doc__ = super().predict_processed.__doc__  # noqa: F841
        return self.base.predict_processed(state, action, next_state, done, **kwargs)


class RewardNetWithVariance(RewardNet):
    """A reward net that keeps track of its epistemic uncertainty through variance."""

    @abc.abstractmethod
    def predict_reward_moments(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Compute the mean and variance of the reward distribution.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            * Estimated reward mean of shape `(batch_size,)`.
            * Estimated reward variance of shape `(batch_size,)`. # noqa: DAR202
        """


class BasicRewardNet(RewardNet):
    """MLP that takes as input the state, action, next state and done flag.

    These inputs are flattened and then concatenated to one another. Each input
    can enabled or disabled by the `use_*` constructor keyword arguments.
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        use_state: bool = True,
        use_action: bool = True,
        use_next_state: bool = False,
        use_done: bool = False,
        **kwargs,
    ):
        """Builds reward MLP.

        Args:
            observation_space: The observation space.
            action_space: The action space.
            use_state: should the current state be included as an input to the MLP?
            use_action: should the current action be included as an input to the MLP?
            use_next_state: should the next state be included as an input to the MLP?
            use_done: should the "done" flag be included as an input to the MLP?
            kwargs: passed straight through to `build_mlp`.
        """
        super().__init__(observation_space, action_space)
        combined_size = 0

        self.use_state = use_state
        if self.use_state:
            combined_size += preprocessing.get_flattened_obs_dim(observation_space)

        self.use_action = use_action
        if self.use_action:
            combined_size += preprocessing.get_flattened_obs_dim(action_space)

        self.use_next_state = use_next_state
        if self.use_next_state:
            combined_size += preprocessing.get_flattened_obs_dim(observation_space)

        self.use_done = use_done
        if self.use_done:
            combined_size += 1

        full_build_mlp_kwargs = {
            "hid_sizes": (32, 32),
        }
        full_build_mlp_kwargs.update(kwargs)
        full_build_mlp_kwargs.update(
            {
                # we do not want these overridden
                "in_size": combined_size,
                "out_size": 1,
                "squeeze_output": True,
            },
        )

        self.mlp = networks.build_mlp(**full_build_mlp_kwargs)

    def forward(self, state, action, next_state, done):
        inputs = []
        if self.use_state:
            inputs.append(th.flatten(state, 1))
        if self.use_action:
            inputs.append(th.flatten(action, 1))
        if self.use_next_state:
            inputs.append(th.flatten(next_state, 1))
        if self.use_done:
            inputs.append(th.reshape(done, [-1, 1]))

        inputs_concat = th.cat(inputs, dim=1)

        outputs = self.mlp(inputs_concat)
        assert outputs.shape == state.shape[:1]

        return outputs


class NormalizedRewardNet(RewardNetWrapper):
    """A reward net that normalizes the output of its base network."""

    def __init__(
        self,
        base: RewardNet,
        normalize_output_layer: Type[nn.Module],
    ):
        """Initialize the NormalizedRewardNet.

        Args:
            base: a base RewardNet
            normalize_output_layer: The class to use to normalize rewards. This
                can be any nn.Module that preserves the shape; e.g. `nn.Identity`,
                `nn.LayerNorm`, or `networks.RunningNorm`.
        """
        # Note(yawen): by default, the reward output is squeezed to produce
        # tensors with (N,) shape for predict_processed. This works for
        # `networks.RunningNorm`, but not for `nn.BatchNorm1d` that requires
        # shape of (N,C).
        super().__init__(base=base)
        # Assuming reward is scalar, norm layer should be initialized with shape (1,).
        self.normalize_output_layer = normalize_output_layer(1)

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
        update_stats: bool = True,
        **kwargs,
    ) -> np.ndarray:
        """Compute normalized rewards for a batch of transitions without gradients.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.
            update_stats: Whether to update the running stats of the normalization
                layer.
            **kwargs: kwargs passed to base predict_processed call.

        Returns:
            Computed normalized rewards of shape `(batch_size,`).
        """
        with networks.evaluating(self):
            # switch to eval mode (affecting normalization, dropout, etc)
            rew_th = th.tensor(
                self.base.predict_processed(state, action, next_state, done, **kwargs),
                device=self.device,
            )
            rew = self.normalize_output_layer(rew_th).detach().cpu().numpy().flatten()
        if update_stats:
            with th.no_grad():
                self.normalize_output_layer.update_stats(rew_th)
        assert rew.shape == state.shape[:1]
        return rew

    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ):
        return self.base(state, action, next_state, done)


class ShapedRewardNet(RewardNetWrapper):
    """A RewardNet consisting of a base network and a potential shaping."""

    def __init__(
        self,
        base: RewardNet,
        potential: Callable[[th.Tensor], th.Tensor],
        discount_factor: float,
    ):
        """Setup a ShapedRewardNet instance.

        Args:
            base: the base reward net to which the potential shaping
                will be added.
            potential: A callable which takes
                a batch of states (as a PyTorch tensor) and returns a batch of
                potentials for these states. If this is a PyTorch Module, it becomes
                a submodule of the ShapedRewardNet instance.
            discount_factor: discount factor to use for the potential shaping.
        """
        super().__init__(
            base=base,
        )
        self.potential = potential
        self.discount_factor = discount_factor

    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ):
        base_reward_net_output = self.base(state, action, next_state, done)
        new_shaping_output = self.potential(next_state).flatten()
        old_shaping_output = self.potential(state).flatten()
        # NOTE(ejnnr): We fix the potential of terminal states to zero, which is
        # necessary for valid potential shaping in a variable-length horizon setting.
        #
        # In more detail: variable-length episodes are usually modeled
        # as infinite-length episodes where we transition to a terminal state
        # in which we then remain forever. The transition to this final
        # state contributes gamma * Phi(s_T) - Phi(s_{T - 1}) to the returns,
        # where Phi is the potential and s_T the final state. But on every step
        # afterwards, the potential shaping leads to a reward of (gamma - 1) * Phi(s_T).
        # The discounted series of these rewards, which is added to the return,
        # is gamma / (1 - gamma) times this reward, i.e. just -gamma * Phi(s_T).
        # This cancels the contribution of the final state to the last "real"
        # transition, so instead of computing the infinite series, we can
        # equivalently fix the final potential to zero without loss of generality.
        # Not fixing the final potential to zero and also not adding this infinite
        # series of remaining potential shapings can lead to reward shaping
        # that does not preserve the optimal policy if the episodes have variable
        # length!
        new_shaping = (1 - done.float()) * new_shaping_output
        final_rew = (
            base_reward_net_output
            + self.discount_factor * new_shaping
            - old_shaping_output
        )
        assert final_rew.shape == state.shape[:1]
        return final_rew


class BasicShapedRewardNet(ShapedRewardNet):
    """Shaped reward net based on MLPs.

    This is just a very simple convenience class for instantiating a BasicRewardNet
    and a BasicPotentialShaping and wrapping them inside a ShapedRewardNet.
    Mainly exists for backwards compatibility after
    https://github.com/HumanCompatibleAI/imitation/pull/311
    to keep the scripts working.

    TODO(ejnnr): if we ever modify AIRL so that it takes in a RewardNet instance
        directly (instead of a class and kwargs) and instead instantiate the
        RewardNet inside the scripts, then it probably makes sense to get rid
        of this class.

    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        *,
        reward_hid_sizes: Sequence[int] = (32,),
        potential_hid_sizes: Sequence[int] = (32, 32),
        use_state: bool = True,
        use_action: bool = True,
        use_next_state: bool = False,
        use_done: bool = False,
        discount_factor: float = 0.99,
        **kwargs,
    ):
        """Builds a simple shaped reward network.

        Args:
            observation_space: The observation space.
            action_space: The action space.
            reward_hid_sizes: sequence of widths for the hidden layers
                of the base reward MLP.
            potential_hid_sizes: sequence of widths for the hidden layers
                of the potential MLP.
            use_state: should the current state be included as an input
                to the reward MLP?
            use_action: should the current action be included as an input
                to the reward MLP?
            use_next_state: should the next state be included as an input
                to the reward MLP?
            use_done: should the "done" flag be included as an input to the reward MLP?
            discount_factor: discount factor for the potential shaping.
            kwargs: passed straight through to `BasicRewardNet` and `BasicPotentialMLP`.
        """
        base_reward_net = BasicRewardNet(
            observation_space=observation_space,
            action_space=action_space,
            use_state=use_state,
            use_action=use_action,
            use_next_state=use_next_state,
            use_done=use_done,
            hid_sizes=reward_hid_sizes,
            **kwargs,
        )

        potential_net = BasicPotentialMLP(
            observation_space=observation_space,
            hid_sizes=potential_hid_sizes,
            **kwargs,
        )

        super().__init__(
            base_reward_net,
            potential_net,
            discount_factor=discount_factor,
        )


class BasicPotentialMLP(nn.Module):
    """Simple implementation of a potential using an MLP."""

    def __init__(
        self,
        observation_space: gym.Space,
        hid_sizes: Iterable[int],
        **kwargs,
    ):
        """Initialize the potential.

        Args:
            observation_space: observation space of the environment.
            hid_sizes: widths of the hidden layers of the MLP.
            kwargs: passed straight through to `build_mlp`.
        """
        super().__init__()
        potential_in_size = preprocessing.get_flattened_obs_dim(observation_space)
        self._potential_net = networks.build_mlp(
            in_size=potential_in_size,
            hid_sizes=hid_sizes,
            squeeze_output=True,
            flatten_input=True,
            **kwargs,
        )

    def forward(self, state: th.Tensor) -> th.Tensor:
        return self._potential_net(state)


class RewardEnsemble(RewardNetWithVariance):
    """A mean ensemble of reward networks."""

    members: nn.ModuleList

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        num_members: int = 5,
        member_cls: Type[RewardNet] = BasicRewardNet,
        member_kwargs: Mapping[str, Any] = {},
        member_normalize_output_layer: Optional[Type[nn.Module]] = None,
        **kwargs,
    ):
        """Initialize the RewardEnsemble.

        Args:
            observation_space: the observation space of the environment
            action_space: the action space of the environment
            num_members: the number of members in the ensemble. Must be at least 1.
            member_cls: class of the constituent reward networks
            member_kwargs: keyword arguments to pass to the ensemble members
            member_normalize_output_layer: The normalization layer to use for the
                member classes. Defaults to None.
            **kwargs: ignored

        Raises:
            ValueError: if num_members is less than 1
        """
        del kwargs
        super().__init__(observation_space, action_space)
        if num_members < 1:
            raise ValueError("Must be at least 1 member in the ensemble.")

        self.members = nn.ModuleList(
            make_reward_net(
                observation_space,
                action_space,
                member_cls,
                member_kwargs,
                member_normalize_output_layer,
            )
            for _ in range(num_members)
        )

    @property
    def num_members(self):
        """The number of members in the ensemble."""
        return len(self.members)

    def forward_all(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ):
        """Return the results reward from each member of the ensemble.

        Args:
            state: The current state as a torch tensor
            action: The current action as a torch tensor
            next_state: The next state as a torch tensor
            done: The done flags as a torch tensor

        Returns:
            The reward given by each ensemble member. This tenor has a shape of
                `(batch_size, num_members)`.
        """
        rewards = []
        for net in self.members:
            rewards.append(net(state, action, next_state, done))
        rewards = th.stack(rewards, dim=-1)
        return rewards

    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ) -> th.Tensor:
        """Compute rewards the mean of all ensemble members.

        Note: This should not be used to to train the ensemble directly! This is because
        the mean of each members loss almost never equals the loss of their mean.
        """
        return self.forward_all(state, action, next_state, done).mean(-1)

    @th.no_grad()
    def predict_reward_moments(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Compute the standard deviation of the reward distribution for a batch.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed reward std of shape `(batch_size,)`.
        """
        state, action, next_state, done = self.preprocess(
            state,
            action,
            next_state,
            done,
        )

        all_rewards = self.forward_all(state, action, next_state, done)
        return all_rewards.mean(-1).cpu().numpy(), all_rewards.var(-1).cpu().numpy()


class AddSTDRewardWrapper(RewardNetWrapper):
    """That adds a multiple of the standard deviation to the reward function."""

    base: RewardNetWithVariance

    def __init__(self, base: RewardNetWithVariance, default_alpha: float = 0.0):
        """Create a conservative reward network.

        Args:
            base: An uncertain rewarard network
            default_alpha: multiple of standard deviation to add to the reward mean.
                Defaults to 0.0.
        """
        super().__init__(base)
        self.default_alpha = default_alpha

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
        alpha: Optional[float] = None,
        **kwargs,
    ) -> np.ndarray:
        """Compute a lower confidence bound on the reward without gradients.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.
            alpha: multiple of standard deviation to add to the reward mean. Defaults
                to the value provided at initialization.
            **kwargs: are not used

        Returns:
            Estimated lower confidence bounds on rewards of shape `(batch_size,`).
        """
        del kwargs

        if alpha is None:
            alpha = self.default_alpha

        reward_mean, reward_var = self.base.predict_reward_moments(
            state,
            action,
            next_state,
            done,
        )

        return reward_mean + alpha * np.sqrt(reward_var)

    def forward(self, *args):
        return self.base.forward(*args)


def make_reward_net(
    observation_space: gym.Space,
    action_space: gym.Space,
    net_cls: Type[RewardNet],
    net_kwargs: Mapping[str, Any],
    normalize_output_layer: Type[nn.Module],
    add_std_alpha: Optional[float] = None,
) -> RewardNet:
    """Builds a reward network.

    Args:
        observation_space: the observation space of the environment.
        action_space: the action space of the environment.
        net_cls: Class of reward network to construct.
        net_kwargs: Keyword arguments passed to reward network constructor.
        normalize_output_layer: Wrapping the reward_net with NormalizedRewardNet
            to normalize the reward output.
        add_std_alpha: multiple of reward function standard deviation to add to the
            reward in predict_processed. Must be None when using a reward function that
            does not keep track of variance. Defaults to None.

    Returns:
        A, possibly wrapped, instance of `net_cls`.

    Raises:
        ValueError: if you try to specify add_std_alpha for a reward network that
            is not an instance of `RewardNetWithVariance`.
    """
    reward_net = net_cls(
        observation_space,
        action_space,
        **net_kwargs,
    )

    if add_std_alpha is not None:
        if isinstance(reward_net, RewardNetWithVariance):
            reward_net = AddSTDRewardWrapper(reward_net, default_alpha=add_std_alpha)
        else:
            raise ValueError(
                "Cannot add standard deviation to reward net that "
                "is not an instance of RewardNetWithVariance!",
            )

    if normalize_output_layer is not None:
        reward_net = NormalizedRewardNet(
            reward_net,
            normalize_output_layer,
        )

    return reward_net
