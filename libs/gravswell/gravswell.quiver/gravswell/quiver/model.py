from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from gravswell.quiver import Platform, conventions
from gravswell.quiver.exporters import Exporter
from gravswell.quiver.model_config import ModelConfig

if TYPE_CHECKING:
    from gravswell.quiver import ModelRepository
    from gravswell.quiver.io.file_system import FileSystem
    from gravswell.quiver.types import EXPOSED_TYPE, SHAPE_TYPE


@dataclass
class ExposedTensor:
    """An input or output tensor to a model.

    Captures descriptive information about inputs
    and outputs of models to allow for easier
    piping between models in ensembles.

    Args:
        model:
            The `Model` to which this tensor belongs
        name:
            The name of this tensor
        shape:
            The shape of this tensor
    """

    model: "Model"
    name: str
    shape: "SHAPE_TYPE"


@dataclass
class Model:
    """An entry within a model repository

    Args:
        name:
            The name of the model
        repository:
            The model repository to which this model belongs
        platform:
            The backend platform used to execute inference
            for this model

    Attributes:
        config:
            The config associated with this model. If one
            already exists in the model repository at
            initialization, it is loaded in and verified
            against the initialization parameters. Otherwise,
            one is initialized using only the `Model`
            initialization params. Information about model
            inputs and outputs is inferred at the time of
            the first version export, or can be specified
            directly onto the config.
    """

    name: str
    repository: "ModelRepository"
    platform: Platform

    def __new__(
        cls,
        name: str,
        repository: "ModelRepository",
        platform: Platform,
    ):
        if platform == Platform.Ensemble:
            cls = EnsembleModel
        return super().__new__(cls)

    def __post_init__(self):
        self.repository.fs.soft_makedirs(self.name)
        self.config = ModelConfig(self)

    @property
    def fs(self) -> "FileSystem":
        """The `FileSystem` leveraged by the model's repository"""

        return self.repository.fs

    @property
    def versions(self) -> list[int]:
        """The existing versions of this model in the repository"""

        # TODO: implement a `walk` method on the filesystems,
        # that way cloud based ones don't have to do two object
        # listings here: one for list and one implicitly inside isdir
        versions = []
        for f in self.fs.list(self.name):
            if self.fs.isdir(f):
                try:
                    version = int(f)
                except ValueError:
                    continue
                versions.append(version)
        return versions

    @property
    def inputs(self) -> dict[str, ExposedTensor]:
        """The inputs exposed by this model

        Represented by a dictionary mapping from the name
        of each input to the corresponding `ExposedTensor`
        """
        inputs = {}
        for input in self.config.input:
            shape = tuple(x if x != -1 else None for x in input.dims)
            inputs[input.name] = ExposedTensor(self, input.name, shape)
        return inputs

    @property
    def outputs(self) -> dict[str, ExposedTensor]:
        """The outputs exposed by this model

        Represented by a dictionary mapping from the name
        of each output to the corresponding `ExposedTensor`
        """
        outputs = {}
        for output in self.config.output:
            shape = tuple(x if x != -1 else None for x in output.dims)
            outputs[output.name] = ExposedTensor(self, output.name, shape)
        return outputs

    def _find_exporter(self, model_fn: Union[Callable, "Model"]) -> Exporter:
        """Find an Exporter capable of exporting the given model function

        Recursively iterates through all sublcasses of the `Exporter`
        class to find the first for which `model_fn` is an instance of
        `Exporter.handles` and for which `model.platform == Exporter.platform`

        Args:
            model_fn:
                The framework-specific function which performs the
                neural network's input/output mapping
        Returns:
            An exporter to export `model_fn` to the format specified
            by this models' inference platform
        """

        def _get_all_subclasses(cls):
            """Utility function for recursively finding all Exporters."""
            all_subclasses = []
            for subclass in cls.__subclasses__():
                if hasattr(subclass, "name"):
                    all_subclasses.append(subclass)
                all_subclasses.extend(_get_all_subclasses(subclass))
            return all_subclasses

        # first try to find an available exporter
        # than can map from the provided model function
        # to the desired inference platform
        for exporter in _get_all_subclasses(Exporter):
            if exporter.platform == self.platform:
                try:
                    if isinstance(model_fn, exporter.handles):
                        # if this exporter matches our criteria,
                        # initialize it with this model and return it
                        return exporter(self)
                except ImportError:
                    # evidently we don't have whatever it handles
                    # installed, so move on
                    continue
        else:
            raise TypeError(
                "No registered exporters which map from "
                "model function type {} to platform {}".format(
                    type(model_fn), self.platform
                )
            )

    def export_version(
        self,
        model_fn: Union[Callable, "Model"],
        version: Optional[int] = None,
        input_shapes: Optional[dict[str, "SHAPE_TYPE"]] = None,
        output_names: Optional[Sequence[str]] = None,
        verbose: int = 0,
        **kwargs,
    ) -> str:
        """Export a version of this model to the repository

        Exports a model represented by `model_fn` to the
        model repository at the specified `version`.

        Args:
            model_fn:
                A framework-specific callable which performs the neural
                network input/output mapping.
            version:
                The version to give this model in the repository.
                If left as `None`, it will be given an index representing
                the latest possible version.
            input_shapes:
                A dictionary mapping from tensor names to shapes. Only
                required for certain export platforms, consult the
                relevant documentation.
            output_names:
                A sequence of names to assign to output tensors
                from the model. Only required for certain export
                platforms, consult the relevant documentation.
            verbose:
                Controls the level of logging verbosity during
                export. Not really being utilized right now.
            **kwargs:
                Any other keyword arguments to pass to `Exporter.export`.
                Consult the relevant documentation.
        """

        # first find an exporter than can do the
        # appropriate model_fn -> inference platform mapping
        exporter = self._find_exporter(model_fn)

        # default version will be the latest
        if version is None:
            version = max(self.versions) + 1

        # create a directory for the current version
        # if it doesn't already exist
        # use boolean returned by soft_makedirs to
        # make sure we remove any directory we created
        # for this version if the export fails
        output_dir = self.fs.join(self.name, str(version))
        do_remove = self.fs.soft_makedirs(output_dir)

        try:
            # first validate that any input shapes we provided
            # match any specified in the existing model config
            exporter._check_exposed_tensors("input", input_shapes)

            # infer the names and shapes of the outputs
            # of the model_fn and ensure that they match
            # any outputs specified in the config
            output_shapes = exporter._get_output_shapes(model_fn, output_names)
            exporter._check_exposed_tensors("output", output_shapes)

            # export the model to the path required by the
            # platform and write the config for good measure
            export_path = self.fs.join(output_dir, conventions[self.platform])
            export_path = exporter._export(
                model_fn, export_path, verbose, **kwargs
            )
            self.config.write()

        except Exception:
            # if anything goes wrong and we created a directory for
            # the export, make sure to get rid of it before raising
            if do_remove:
                self.fs.remove(output_dir)
            raise


class EnsembleModel(Model):
    """A meta-model linking together inputs and outputs from `Model`s

    An ensemble model is a model in a Triton repository
    which doesn't have any sort of executable associated
    with it, but rather describes a way to connect the
    input and outputs from other normal models in the
    repo.
    """

    @property
    def models(self) -> Model:
        """Returns the models which this enesmble leverages for inference."""
        return [
            self.repository.models[step.model_name]
            for step in self._config.ensemble_scheduling.step
        ]

    def _update_step_map(
        self,
        tensor: ExposedTensor,
        key: str,
        exposed_type: "EXPOSED_TYPE",
    ):
        """Updates the routing of data through the ensemble."""

        for step in self.config.ensemble_scheduling.step:
            if step.model_name == tensor.model.name:
                step_map = getattr(step, exposed_type + "_map")
                step_map[tensor.name] = key

    def add_input(
        self,
        input: ExposedTensor,
        version: Optional[int] = None,
        key: Optional[str] = None,
    ) -> ExposedTensor:
        if input.model not in self.models:
            self.config.add_step(input.model, version=version)

        # create an input using either the specified key if
        # provided, otherwise using the name of the input tensor
        key = key or input.name
        if key not in self.inputs:
            # TODO: dynamic dtype mapping
            self.config.add_input(key, input.shape, dtype="float32")
        else:
            raise ValueError(f"Already added input using key {key}")

        self._update_step_map(input, key, "input")
        return self.inputs[key]

    def add_output(
        self,
        output: ExposedTensor,
        version: Optional[int] = None,
        key: Optional[str] = None,
    ) -> ExposedTensor:
        if output.model not in self.models:
            self.config.add_step(output.model, version=version)

        # create an output using either the specified key if
        # provided, otherwise using the name of the output tensor
        key = key or output.name
        if key not in self.inputs:
            # TODO: dynamic dtype mapping
            self.config.add_output(key, output.shape, dtype="float32")
        else:
            raise ValueError(f"Already added output using key {key}")

        self._update_step_map(output, key, "output")
        return self.outputs[key]

    def add_streaming_inputs(
        self,
        inputs: Union[Sequence[ExposedTensor], ExposedTensor],
        stream_size: int,
        name: Optional[str] = None,
        streams_per_gpu: int = 1,
    ) -> Model:
        if not isinstance(inputs, Sequence):
            inputs = [inputs]

        for input in inputs:
            if input.model not in self.models:
                # TODO: support versions
                self.config.add_step(input.model)

        # Do the import for the streaming code here
        # so that TensorFlow doesn't become a mandatory
        # dependency of the library
        try:
            from exportlib.stream import make_streaming_input_model
        except ImportError as e:
            if "tensorflow" in str(e):
                raise RuntimeError(
                    "Unable to leverage streaming input, "
                    "must install TensorFlow first"
                )

        # add a streaming model to the repository
        # and set up its config with the correct
        # instance group
        streaming_model = make_streaming_input_model(
            self.repository, inputs, stream_size, name, streams_per_gpu
        )

        # add the streaming model's input as an input
        # to this ensemble model.
        # TODO: should we include some sort of optional key
        self.add_input(streaming_model.inputs["stream"])

        # pipe the output of this streaming model to
        # each one of the corresponding inputs. Include
        # some metadata to indicate in which order along
        # the channel axis each one of these lies
        metadata = []
        for tensor, output in zip(inputs, streaming_model.outputs):
            self.pipe(streaming_model.outputs[output.name], tensor)
            metadata.append(f"{tensor.model.name}/{tensor.name}")
        self.config.parameters["states"].string_value = ",".join(metadata)

        # return the streaming model we created
        # TODO: better to return the "stream" input
        # of this model to be more consistent with `add_input`?
        return streaming_model

    def pipe(
        self,
        outbound: ExposedTensor,
        inbound: ExposedTensor,
        key: Optional[str] = None,
    ) -> None:
        # verify that we're connecting tensors of the same shape
        if not outbound.shape == inbound.shape:
            raise ValueError(
                f"Outbound tensor has shape {outbound.shape} which doesn't "
                f"match shape of inbound tensor {inbound.shape}"
            )

        # add the models associated with the
        # inbound and outbound tensors to the
        # ensmble if they aren't in it yet
        for tensor in [inbound, outbound]:
            if tensor.model not in self.models:
                # TODO: support per-model versioning
                self.config.add_step(tensor.model)

        # find the step in the config associated
        # with the outbound tensor
        for step in self.config.ensemble_scheduling.step:
            if step.model_name == outbound.model.name:
                break

        # check to see if this tensor has already been
        # associated with some named mapping.
        existing_key = step.output_map[outbound.name]
        if existing_key == "":
            # the tensor name is not currently associated with
            # any mapping, so create one for it using either
            # the specified key if provided, otherwise using
            # just the tensor name as-is
            key = key or outbound.name
            self._update_step_map(outbound, key, "output")
        else:
            if key is not None and existing_key != key:
                # if we specified a name but the tensor
                # has already been mapped under a different
                # key, raise an error
                raise ValueError(
                    f"Output {outbound.name} from {outbound.model.name} "
                    f"already using key {existing_key}, couldn't "
                    f"use provided key {key}"
                )

            # use the key we've already specified
            # to use in the inbound tensor's input map
            key = existing_key

        # find the step associated with the inbound tensor
        for step in self.config.ensemble_scheduling.step:
            if step.model_name == inbound.model.name:
                break

        # check to see if this tensor has already been
        # given a key
        existing_key = step.input_map[inbound.name]
        if existing_key == "":
            # if not, create an input mapping entry
            # for it using the outbound tensor's key
            self._update_step_map(inbound, key, "input")
        elif existing_key != key:
            # if the outbound tensor's key doesn't match
            # the key that already exists at the inbound
            # tensor's input map, raise an error
            raise ValueError(
                f"Input {inbound.name} to {inbound.model.name} "
                f"already receiving input from key {existing_key}, "
                f"can't pipe input from key {key}"
            )
