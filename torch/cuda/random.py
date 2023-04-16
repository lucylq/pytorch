import torch
from typing import Iterable, List, Union
from . import _lazy_init, _lazy_call, device_count, current_device
from .. import Tensor

__all__ = ['get_rng_state', 'get_rng_state_all',
           'set_rng_state', 'set_rng_state_all',
           'set_rng_state_offset',
           'manual_seed', 'manual_seed_all',
           'seed', 'seed_all', 'initial_seed']



def get_device(device: Union[int, str, torch.device] = 'cuda') -> torch.device:
    if isinstance(device, str):
        device = torch.device(device)
    elif isinstance(device, int):
        device = torch.device('cuda', device)
    return device


def get_generator(device: torch.device) -> torch._C.Generator:
    idx = device.index
    if idx is None:
        idx = current_device()
    return torch.cuda.default_generators[idx]


def get_rng_state(device: Union[int, str, torch.device] = 'cuda') -> Tensor:
    r"""Returns the random number generator state of the specified GPU as a ByteTensor.

    Args:
        device (torch.device or int, optional): The device to return the RNG state of.
            Default: ``'cuda'`` (i.e., ``torch.device('cuda')``, the current CUDA device).

    .. warning::
        This function eagerly initializes CUDA.
    """
    _lazy_init()
    device = get_device(device)
    default_generator = get_generator(device)
    return default_generator.get_state()


def get_rng_state_all() -> List[Tensor]:
    r"""Returns a list of ByteTensor representing the random number states of all devices."""

    results = []
    for i in range(device_count()):
        results.append(get_rng_state(i))
    return results


def set_rng_state(new_state: Tensor, device: Union[int, str, torch.device] = 'cuda') -> None:
    r"""Sets the random number generator state of the specified GPU.

    Args:
        new_state (torch.ByteTensor): The desired state
        device (torch.device or int, optional): The device to set the RNG state.
            Default: ``'cuda'`` (i.e., ``torch.device('cuda')``, the current CUDA device).
    """
    new_state_copy = new_state.clone(memory_format=torch.contiguous_format)
    final_device = get_device(device)

    def cb():
        default_generator = get_generator(final_device)
        default_generator.set_state(new_state_copy)

    _lazy_call(cb)


def set_rng_state_offset(offset: int, device: Union[int, str, torch.device] = 'cuda') -> None:
    r"""Sets the random number generator state offset of the specified GPU.

    Args:
        offset (int): The desired offset
        device (torch.device or int, optional): The device to set the RNG state.
            Default: ``'cuda'`` (i.e., ``torch.device('cuda')``, the current CUDA device).
    """
    final_device = get_device(device)

    def cb():
        default_generator = get_generator(final_device)
        default_generator.set_offset(offset)

    _lazy_call(cb)



def set_rng_state_all(new_states: Iterable[Tensor]) -> None:
    r"""Sets the random number generator state of all devices.

    Args:
        new_states (Iterable of torch.ByteTensor): The desired state for each device"""
    for i, state in enumerate(new_states):
        set_rng_state(state, i)


def manual_seed(seed: int) -> None:
    r"""Sets the seed for generating random numbers for the current GPU.
    It's safe to call this function if CUDA is not available; in that
    case, it is silently ignored.

    Args:
        seed (int): The desired seed.

    .. warning::
        If you are working with a multi-GPU model, this function is insufficient
        to get determinism.  To seed all GPUs, use :func:`manual_seed_all`.
    """
    seed = int(seed)

    def cb():
        idx = current_device()
        default_generator = torch.cuda.default_generators[idx]
        default_generator.manual_seed(seed)

    _lazy_call(cb, seed=True)


def manual_seed_all(seed: int) -> None:
    r"""Sets the seed for generating random numbers on all GPUs.
    It's safe to call this function if CUDA is not available; in that
    case, it is silently ignored.

    Args:
        seed (int): The desired seed.
    """
    seed = int(seed)

    def cb():
        for i in range(device_count()):
            default_generator = torch.cuda.default_generators[i]
            default_generator.manual_seed(seed)

    _lazy_call(cb, seed_all=True)


def seed() -> None:
    r"""Sets the seed for generating random numbers to a random number for the current GPU.
    It's safe to call this function if CUDA is not available; in that
    case, it is silently ignored.

    .. warning::
        If you are working with a multi-GPU model, this function will only initialize
        the seed on one GPU.  To initialize all GPUs, use :func:`seed_all`.
    """
    def cb():
        idx = current_device()
        default_generator = torch.cuda.default_generators[idx]
        default_generator.seed()

    _lazy_call(cb)


def seed_all() -> None:
    r"""Sets the seed for generating random numbers to a random number on all GPUs.
    It's safe to call this function if CUDA is not available; in that
    case, it is silently ignored.
    """
    def cb():
        random_seed = 0
        seeded = False
        for i in range(device_count()):
            default_generator = torch.cuda.default_generators[i]
            if not seeded:
                default_generator.seed()
                random_seed = default_generator.initial_seed()
                seeded = True
            else:
                default_generator.manual_seed(random_seed)

    _lazy_call(cb)


def initial_seed() -> int:
    r"""Returns the current random seed of the current GPU.

    .. warning::
        This function eagerly initializes CUDA.
    """
    _lazy_init()
    idx = current_device()
    default_generator = torch.cuda.default_generators[idx]
    return default_generator.initial_seed()
