import contextlib
import logging
import warnings

from . import utils
from .opt import OptimWrapper
from .scaler import LossScaler, iter_params
from ._amp_state import _amp_state
from ..fp16_utils import FP16_Optimizer


# There's no reason to expose the notion of a "handle". Everything can happen through amp.* calls.
@contextlib.contextmanager
def scale_loss(loss,
               optimizer,
               model=None,
               delay_unscale=False):
    """
    On context manager entrance, scale the loss in a way consistent with the current loss scale.
    Yield the loss

    On context manager exit (if ``delay_unscale=False``), unscale the gradients so that
    ``optimizer.step()`` can be called.

    .. note::
    If Amp is using explicit FP32 master params (which is the default for ``opt_level=O2``, and
    can also be manually enabled by supplying ``master_weights=True`` to ``amp.initialize``)
    any FP16 gradients are copied to FP32 master gradients before being unscaled.  ``optimizer.step()``
    will then apply the unscaled master gradients to the master params.
    """
    if not _amp_state.opt_properties.enabled:
        yield loss
        return

    if optimizer.loss_scaler is None:
        raise RuntimeError("optimizer passed to scale_loss does not have a loss_scaler.")

    loss_scale = optimizer.loss_scaler.loss_scale()

    if ((not _amp_state.opt_properties.master_weights)
        and (not optimizer.loss_scaler.dynamic)
        and loss_scale == 1.0):
        yield loss
        # Needing to drop the cache here as well is an ugly gotcha.
        # But for now I think it's necessary to short-circuit.
        # Probably ok to skip this if not delay_unscale
        if _amp_state.opt_properties.patch_torch_functions:
            _amp_state.handle._clear_cache()
        return

    yield loss*loss_scale

    # this isn't pretty but it unifies things.  Once I deprecate the old API entirely,
    # I will have freedom to clean this up.  Maybe instead of wrapping optimizers,
    # I can simply construct a set of attributes (e.g. master params) and assign them
    # directly to optimizer instances.
    if not delay_unscale:
        if isinstance(optimizer, FP16_Optimizer):
            optimizer.update_master_grads()
        else:
            optimizer.loss_scaler.clear_overflow_state()
            optimizer.loss_scaler.unscale(
                iter_params(optimizer.param_groups),
                iter_params(optimizer.param_groups),
                loss_scale)
            # For future fused optimizers that enable sync-free dynamic loss scaling,
            # should_skip will always be False.
            should_skip = optimizer.loss_scaler.update_scale()
            if should_skip:
                optimizer_step = optimizer.step
                def skip_step():
                    logger = logging.getLogger('apex.amp')
                    logger.warning("Gradient overflow.  Skipping step, reducing " +
                                   "loss scale to {}".format(optimizer.loss_scaler.loss_scale()))
                    optimizer.step = optimizer_step
                optimizer.step = skip_step

    # Probably ok to skip this if not delay_unscale
    if _amp_state.opt_properties.patch_torch_functions:
        _amp_state.handle._clear_cache()


# Free function version of AmpHandle.disable_casts, another step on the
# path to removing the concept of "AmpHandle"
@contextlib.contextmanager
def disable_casts():
    _amp_state.handle._is_active = False
    yield
    _amp_state.handle._is_active = True


class AmpHandle(object):
    def __init__(self, loss_scale="dynamic", enable_caching=True, verbose=False):
        self._enable_caching = enable_caching
        self._verbose = verbose
        self._cache = dict()
        self._default_scaler = LossScaler(loss_scale)
        self._is_active = True
        self._all_wrappers = []

    def is_active(self):
        return self._is_active

    @contextlib.contextmanager
    def _disable_casts(self):
        self._is_active = False
        yield
        self._is_active = True

    def wrap_optimizer(self, optimizer, num_loss=1):
        self._default_scaler = None
        return OptimWrapper(optimizer, self, num_loss)

    @contextlib.contextmanager
    def scale_loss(self, loss, optimizer):
        if not self.is_active():
            yield loss
            return

        if self._default_scaler is None:
            raise RuntimeError(
                'After calling `handle.wrap_optimizer()`, you must explicitly ' +
                'use `optimizer.scale_loss(loss)`.')

        # TODO: this code block is duplicated here and `opt.py`. Unify.
        loss_scale = self._default_scaler.loss_scale()
        yield loss * loss_scale

        self._default_scaler.clear_overflow_state()
        self._default_scaler.unscale(
            iter_params(optimizer.param_groups),
            iter_params(optimizer.param_groups),
            loss_scale)
        should_skip = self._default_scaler.update_scale()
        if should_skip:
            optimizer_step = optimizer.step
            def skip_step():
                logger = logging.getLogger('apex.amp')
                logger.warning('Gradient overflow, skipping update')
                optimizer.step = optimizer_step
            optimizer.step = skip_step

        self._clear_cache()

    def _clear_cache(self):
        self._cache.clear()

    # Experimental support for saving / restoring uncasted versions of functions
    def _save_func(self, mod, fn, func):
        self._all_wrappers.append((mod, fn, func))

    def _deactivate(self):
        for mod, fn, func in self._all_wrappers:
            utils.set_func(mod, fn, func)
        self._all_wrappers = []

    @property
    def has_cache(self):
        return self._enable_caching

    @property
    def cache(self):
        return self._cache

    def remove_cache(self, param):
        if self.has_cache and param in self.cache:
            del self.cache[param]

    @property
    def verbose(self):
        return self._verbose

class NoOpHandle(object):
    def is_active(self):
        return False

    @contextlib.contextmanager
    def _disable_casts(self):
        yield

    def wrap_optimizer(self, optimizer, num_loss=1):
        return OptimWrapper(optimizer, self, num_loss)

    @contextlib.contextmanager
    def scale_loss(self, loss, optimizer):
        yield loss

    @property
    def has_cache(self):
        return False

    @property
    def verbose(self):
        return False

    def _clear_cache(self):
        pass

    def _deactivate(self):
        pass
