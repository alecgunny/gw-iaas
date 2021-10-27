import pickle
from copy import deepcopy
from tempfile import TemporaryDirectory
from typing import Callable, Optional, Sequence, Union

import requests

try:
    from hermes.quiver.exporters.tensorrt.onnx import convert_network

    _has_trt = True
except ImportError as e:
    if "tensorrt" not in str(e):
        raise
    _has_trt = False

from hermes.quiver.exporters.torch_onnx import TorchOnnx, TorchOnnxMeta
from hermes.quiver.model import Model
from hermes.quiver.model_repository import ModelRepository
from hermes.quiver.platform import Platform, conventions
from hermes.quiver.types import SHAPE_TYPE


class TorchTensorRTMeta(TorchOnnxMeta):
    @property
    def handles(self):
        handle = TorchOnnxMeta.handles.fget(self)
        return (handle, Model)

    @property
    def platform(self):
        return Platform.TENSORRT


class TorchTensorRT(TorchOnnx, metaclass=TorchTensorRTMeta):
    def __call__(
        self,
        model_fn: Union[Callable, Model],
        version: int,
        input_shapes: Optional[SHAPE_TYPE] = None,
        output_names: Optional[Sequence[str]] = None,
        use_fp16: bool = False,
        endpoint: Optional[str] = None,
    ):
        if endpoint is None and not _has_trt:
            raise ImportError(
                "Must have  tensorrt installed to use TorchTensorRT "
                "export platform if no conversion endpoint is specified."
            )

        def do_conversion(model_binary, config):
            # merge info about inputs and outputs from
            # the existing config into our config
            # do this in a few steps to be safe:
            # create a copy of this model's config (which
            # may or may not have info about inputs/outputs)
            # TODO: do we just need to merge in inputs/outputs?
            # can we use this with _check_exposed_tensors?
            temp_config = deepcopy(self.config._config)

            # merge in the config from the model
            # we're converting from
            temp_config.MergeFrom(config)

            # then merge back in our config so that
            # things like platform, max_batch_size, etc.
            # are preserved.
            temp_config.MergeFrom(self.config._config)

            # now set our config to this copy's value
            self.config._config = temp_config

            if endpoint is None:
                # do the conversion locally
                engine = convert_network(
                    model_binary, config._config, use_fp16
                )

                # CUDA engine build won't raise an error on failure
                # but will return None instead, so raise error here
                if engine is None:
                    raise RuntimeError(
                        "Model conversion failed, consult TRT logs"
                    )

                # serialize the engine to bytes
                trt_binary = engine.serialize()
            else:
                # use a remote conversion service to convert
                # the onnx binary to a tensorrt one
                data = {
                    "config": self.config._config.SerializeToString(),
                    "network": model_fn,
                }
                response = requests.post(
                    url=endpoint,
                    data=pickle.dumps(data),
                    headers={"Content-Type": "application/octet-stream"},
                )

                response.raise_for_status()
                trt_binary = response.content

            # write the binary file to the appropriate location
            export_path = self.fs.join(
                self.config.name, str(version), conventions[self.platform]
            )
            self.fs.write(trt_binary, export_path)
            return export_path

        if not isinstance(model_fn, Model):
            # if we didn't pass a model, then create a dummy
            # temporary model repository to build the ONNX
            # binary that we'll convert and do export with that
            # TODO: I guess we don't need this if we already
            # have that info in the config
            if input_shapes is None:
                raise ValueError(
                    "Must specify input shapes if passing "
                    "a torch model for export directly"
                )

            # use temporary directory as context so
            # that it will delete no matter what happens
            with TemporaryDirectory() as d:
                repo = ModelRepository(d)
                model = repo.add("tmp", platform=super().platform)
                onnx_path = model.export_version(
                    model_fn,
                    version=version,
                    input_shapes=input_shapes,
                    output_names=output_names,
                )
                with open(onnx_path, "rb") as f:
                    model_binary = f.read()

                return do_conversion(model_binary, model.config._config)
        else:
            onnx_path = model_fn.repository.fs.join(
                model_fn.name, str(version), conventions[self.platform]
            )
            with open(onnx_path, "rb") as f:
                model_binary = f.read()

            return do_conversion(model_binary, model.config._config)
