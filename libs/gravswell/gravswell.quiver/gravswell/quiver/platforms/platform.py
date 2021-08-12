import abc
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from abc.collections import Sequence
    from typing import Callable, Optional, Union

    from gravswell.quiver import Model
    from gravswell.quiver.types import EXPOSED_TYPE, SHAPE_TYPE


_SHAPES_TYPE = Optional[Sequence[SHAPE_TYPE], dict[str, SHAPE_TYPE]]


@dataclass
class PlatformConvention:
    """Describes the expected nomenclature for a particular inference platform

    Triton by default expects each inference
    platform to be associated with a particular
    filename for the model versions hosted
    in the repository. This is meant as a map to
    associate the two.

    Args:
        name: The name of the platform as it
            appears in the Triton model config
        filename: The associated expected filename
    """

    name: str
    filename: str


class Convention(Enum):
    """Enumerates the existing conventions for each inference platform"""

    ONNX = PlatformConvention("onnxruntime_onnx", "model.onnx")
    SAVEDMODEL = PlatformConvention(
        "tensorflow_savedmodel", "model.savedmodel"
    )
    TENSORRT = PlatformConvention("tensorrt_plan", "model.plan")
    ENSEMBLE = PlatformConvention("ensemble", "model.empty")


@dataclass
class Platform(metaclass=abc.ABCMeta):
    """
    Metaclass for implementing export platforms.
    Should not be instantiated on its own.

    Args:
        model: The `Model` which will be exported
            using this platform
    """

    model: Model

    def _check_exposed_tensors(
        self, exposed_type: EXPOSED_TYPE, provided: _SHAPES_TYPE = None
    ) -> None:
        """
        Perform some checks on the provided input
        or output shapes to make sure they align
        with the shapes specified in the model config
        if there are any. If there aren't any shapes
        currently specified in the model config, the
        `provided` shapes will be inserted. Inconsistent
        shapes will raise errors, otherwise `None` is returned.

        Args:
            exposed_type: The type of tensor whose shapes
                we're checking, either model inputs or
                outputs
            provided: Any shapes that were provided explicitly
                to the platform for exporting. If provided as
                a `Sequence`, the corresponding tensor names
                will be assigned generically, and comparison
                will happen with the existing `Config.input`
                in order. If provided as a `dict` mapping from
                tensor names to shapes, shapes will be validated
                using the corresponding name. If left as `None`,
                shapes will be inferred from the existing model
                config. If there is no model config, this will
                raise a `ValueError`
        """

        # get any information about the input/output
        # shapes from the existing model config
        exposed = getattr(self.model.config, exposed_type)
        if len(exposed) == 0 and provided is None:
            # our config doesn't have any exposed tensors
            # already, and we haven't provided any
            # raise an error because we don't have any
            # way to infer shapes to write to the config
            raise ValueError("Must specify {} shapes".format(exposed_type))
        elif len(exposed) == 0:
            # our config doesn't have any exposed tensors,
            # but we've provided some, so add them to the
            # config
            if not isinstance(provided, dict):
                # if all we did was provide a sequence of
                # shapes, name them in a generic way assigning
                # indexed postfixes to "input" or "output"
                provided = {
                    f"{exposed_type}_{i}": shape
                    for i, shape in enumerate(provided)
                }

            for name, shape in provided.items():
                # check to make sure that any dimensions
                # beyond the batch dimension are valid
                # TODO: support variable length axes beyond
                # just the batch dimension
                if any([i is None for i in shape[1:]]):
                    raise ValueError(
                        "Shape {} has variable length axes outside "
                        "of the first dimension. This isn't allowed "
                        "at the moment".format(shape)
                    )

                # add either an input our output to the model config
                # in a generic way by grabbing the method programmatically
                add_fn = getattr(self.model.config, "add_" + exposed_type)

                # TODO: don't hardcode dtype
                add_fn(name=name, shape=shape, dtype="float32")
        elif provided is not None:
            # our config has some exposed tensors already, and
            # we've provided some, so make sure everything matches
            if not isinstance(provided, dict):
                # if we provided a list of shapes, iterate
                # through the inputs/outputs in the config
                # in order and assume they're meant to match
                provided = {
                    x.name: shape for x, shape in zip(exposed, provided)
                }

            if len(provided) != len(exposed) or set(provided) != set(
                [x.name for x in exposed]
            ):
                raise ValueError(
                    "Provided {exposed_type}s {provided} "
                    "don't match config {exposed_type}s {config}".format(
                        exposed_type=exposed_type,
                        provided=list(provided.keys()),
                        config=[x.name for x in exposed],
                    )
                )

            # next check that the shapes match
            for ex in exposed:
                config_shape = list(ex.dims)

                # map `None` in provided shape to `-1`
                # for consistency with Triton conventions
                provided_shape = [i or -1 for i in provided[ex.name]]
                if config_shape != provided_shape:
                    # the shape we specified doesn't match the
                    # shape found in the existing config, so
                    # raise an error
                    raise ValueError(
                        "Shapes {}, {} don't match".format(
                            tuple(config_shape), tuple(provided_shape)
                        )
                    )

    def _validate_platform(self) -> None:
        return

    def export(
        self,
        model_fn: Callable,
        version: int,
        input_shapes: _SHAPES_TYPE = None,
        output_names: Optional[list[str]] = None,
        verbose: int = 0,
    ) -> str:
        # first validate whether this platform can
        # be used in the given environment. Don't
        # do this at initialization time because we
        # might need to work with a repo which hosts
        # other models we can't support, but it doesn't
        # matter so long as we don't try to do an
        # export with them
        self._validate_platform()

        # first validate that any input shapes
        # we provided match any specified
        # in the existing model config
        self._check_exposed_tensors("input", input_shapes)

        # infer the names and shapes of the outputs
        # of the model_fn and ensure that they match
        # any outputs specified in the config
        output_shapes = self._get_output_shapes(model_fn, output_names)
        self._check_exposed_tensors("output", output_shapes)

        # export the model to the path required
        # by the platform and write the config
        # for good measure
        export_path = self.model.fs.join(
            self.model.name, str(version), self.convention.filename
        )
        export_path = self._export(model_fn, export_path, verbose)
        self.model.config.write()

        # return the path to which the model
        # was exported
        return export_path

    @abc.abstractmethod
    def _get_output_shapes(
        self, model_fn: Callable, output_names: Optional[list[str]]
    ) -> Union[Sequence[SHAPE_TYPE], dict[str, SHAPE_TYPE]]:
        """Infer the output shapes for the model

        Uses the `model_fn` and input names and shapes
        as specified in the associated model config
        to infer the shapes of the model outputs.

        Args:
            model_fn:
                The function which maps model inputs
                to outputs. Subclasses may accept
                framework-specific equivalents like
                `torch.nn.Module` and `tensorflow.keras.Model`
            output_names:
                The associated names of the outputs. If
                specified, they will be assumed to be ordered
                in the same order that their corresponding
                outputs are returned by `model_fn`. The return
                value will then be a dictionary mapping these
                names to the corresponding shape. Otherwise,
                the output shapes will be returned as a list
                in the order in which they are returned by
                `model_fn`

        Returns:
            Shapes of the model outputs either in a list
            or a dictionary mapping from the output name
            to the shape.
        """

        pass

    @abc.abstractproperty
    def convention(self) -> Convention:
        pass

    @abc.abstractmethod
    def _do_export(self, model_fn, export_dir, verbose=0):
        pass
