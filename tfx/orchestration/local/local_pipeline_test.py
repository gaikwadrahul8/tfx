# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""End to end test for running a custom component based pipeline in local mode.

The component and pipeline pattern in this file are provided only for testing
purposes and are not a recommended way to structure TFX pipelines. We recommend
consulting the TFX Component Tutorial
(https://www.tensorflow.org/tfx/tutorials) for a
recommended pipeline topology.
"""
# pylint: disable=invalid-name,no-value-for-parameter

import collections
import json
import os
import tempfile
from typing import Any, List

import absl.testing.absltest
import apache_beam as beam
from apache_beam.options.pipeline_options import DirectOptions

from tfx import types
from tfx.dsl.compiler import compiler
from tfx.dsl.component.experimental.annotations import BeamComponentParameter
from tfx.dsl.component.experimental.annotations import InputArtifact
from tfx.dsl.component.experimental.annotations import OutputArtifact
from tfx.dsl.component.experimental.annotations import OutputDict
from tfx.dsl.component.experimental.annotations import Parameter
from tfx.dsl.component.experimental.decorators import component
from tfx.dsl.io import fileio
from tfx.dsl.placeholder import placeholder as ph
from tfx.orchestration import pipeline as pipeline_py
from tfx.orchestration.local import local_dag_runner
from tfx.orchestration.metadata import sqlite_metadata_connection_config
from tfx.proto.orchestration import pipeline_pb2, placeholder_pb2


class DummyDataset(types.Artifact):
  TYPE_NAME = 'DummyDataset'

  def read(self) -> List[Any]:
    with fileio.open(os.path.join(self.uri, 'dataset.json')) as f:
      return json.load(f)

  def write(self, data: List[Any]):
    with fileio.open(os.path.join(self.uri, 'dataset.json'), 'w+') as f:
      json.dump(data, f)


class DummyModel(types.Artifact):
  TYPE_NAME = 'DummyModel'

  def read(self) -> 'SimpleModel':
    return SimpleModel.read_from(self.uri)

  def write(self, model_obj: 'SimpleModel') -> None:
    model_obj.write_to(self.uri)


@component
def LoadDummyDatasetComponent(dataset: OutputArtifact[DummyDataset]):
  dataset.write(['A', 'B', 'C', 'C', 'C'])
  LocalDagRunnerTest.RAN_COMPONENTS.append('Load')


@component(use_beam=True)
def SimpleBeamPoweredComponent(beam_pipeline: BeamComponentParameter[beam.Pipeline] = None):
  with beam_pipeline as p:
    print(p.options.view_as(DirectOptions))
    direct_num_workers = p.options.view_as(DirectOptions).direct_num_workers
    direct_running_mode = p.options.view_as(DirectOptions).direct_running_mode
    LocalDagRunnerTest.BEAM_ARG_VALUES['direct_num_workers'] = direct_num_workers
    LocalDagRunnerTest.BEAM_ARG_VALUES['direct_running_mode'] = direct_running_mode


class SimpleModel:
  """Simple model that always predicts a set prediction."""

  def __init__(self, always_predict: Any):
    self.always_predict = always_predict

  @classmethod
  def read_from(cls, model_uri: str) -> 'SimpleModel':
    with fileio.open(os.path.join(model_uri, 'model_data.json')) as f:
      data = json.load(f)
    return cls(data['prediction'])

  def write_to(self, model_uri: str) -> None:
    data = {'prediction': self.always_predict}
    with fileio.open(os.path.join(model_uri, 'model_data.json'), 'w+') as f:
      json.dump(data, f)


# Fake loss and accuracy value returned by training procedure.
_DUMMY_LOSS = 0.12345
_DUMMY_ACCURACY = 0.6


def train_dummy_model(records, unused_num_iterations):
  seen_count = collections.defaultdict(int)
  most_seen_count = 0
  most_seen_record = None
  for record in records:
    seen_count[record] += 1
    if seen_count[record] > most_seen_count:
      most_seen_count = seen_count[record]
      most_seen_record = record
  accuracy = most_seen_count / len(records)
  assert accuracy == _DUMMY_ACCURACY
  return (SimpleModel(most_seen_record), _DUMMY_LOSS, accuracy)


@component
def DummyTrainComponent(
    training_data: InputArtifact[DummyDataset],
    model: OutputArtifact[DummyModel],
    num_iterations: Parameter[int] = 10) -> OutputDict(
        loss=float, accuracy=float):
  """Simple fake trainer component."""

  records = training_data.read()
  model_obj, loss, accuracy = train_dummy_model(records, num_iterations)
  model.write(model_obj)

  LocalDagRunnerTest.RAN_COMPONENTS.append('Train')

  return {
      'loss': loss,
      'accuracy': accuracy,
  }


@component
def DummyValidateComponent(model: InputArtifact[DummyModel], loss: float,
                           accuracy: float):
  """Validation component for fake trained model."""
  prediction = model.read().always_predict
  assert prediction == 'C', prediction
  assert loss == _DUMMY_LOSS, loss
  assert accuracy == _DUMMY_ACCURACY, accuracy

  LocalDagRunnerTest.RAN_COMPONENTS.append('Validate')


class LocalDagRunnerTest(absl.testing.absltest.TestCase):

  # Global list of components names that have run, used to confirm
  # execution side-effects in local test.
  RAN_COMPONENTS = []
  # List of beam env vars from placeholders
  BEAM_ARG_VALUES = None

  def setUp(self):
    super().setUp()
    self.__class__.RAN_COMPONENTS = []
    self.__class__.BEAM_ARG_VALUES = {}

  def _getTestPipeline(self) -> pipeline_py.Pipeline:
    # Construct component instances.
    dummy_load_component = LoadDummyDatasetComponent().with_id('Load')
    dummy_train_component = DummyTrainComponent(
        training_data=dummy_load_component.outputs['dataset'],
        num_iterations=5).with_id('Train')
    dummy_validate_component = DummyValidateComponent(
        model=dummy_train_component.outputs['model'],
        loss=dummy_train_component.outputs['loss'],
        accuracy=dummy_train_component.outputs['accuracy']).with_id('Validate')

    # Construct and run pipeline
    temp_path = tempfile.mkdtemp()
    pipeline_root_path = os.path.join(temp_path, 'pipeline_root')
    metadata_path = os.path.join(temp_path, 'metadata.db')
    return pipeline_py.Pipeline(
        pipeline_name='test_pipeline',
        pipeline_root=pipeline_root_path,
        metadata_connection_config=sqlite_metadata_connection_config(
            metadata_path),
        components=[
            dummy_load_component,
            dummy_train_component,
            dummy_validate_component,
        ])

  def _getTestPipelineIR(self) -> pipeline_pb2.Pipeline:
    test_pipeline = self._getTestPipeline()
    c = compiler.Compiler()
    return c.compile(test_pipeline)

  def _getTestBeamComponentPipeline(self, num_workers_env_var_name,
                                    direct_running_mode_env_var_name) \
          -> pipeline_py.Pipeline:
    # Construct component instances.
    dummy_beam_component = SimpleBeamPoweredComponent().with_id('Beam')

    # Construct and run pipeline
    temp_path = tempfile.mkdtemp()
    pipeline_root_path = os.path.join(temp_path, 'pipeline_root')
    metadata_path = os.path.join(temp_path, 'metadata.db')
    return pipeline_py.Pipeline(
        pipeline_name='test_pipeline',
        pipeline_root=pipeline_root_path,
        metadata_connection_config=sqlite_metadata_connection_config(
            metadata_path),
        components=[dummy_beam_component],
        beam_pipeline_args=['--runner=DirectRunner',
                            # '--direct_running_mode=multi_processing',
                            '--direct_running_mode=' + ph.Placeholder(
                                placeholder_type=placeholder_pb2.Placeholder.ENVIRONMENT_VARIABLE,
                                key=direct_running_mode_env_var_name),
                            ph.Placeholder(placeholder_type=placeholder_pb2.Placeholder.ENVIRONMENT_VARIABLE,
                                           key=num_workers_env_var_name)
                            ],
    )

  def testSimplePipelineRun(self):
    self.assertEqual(self.RAN_COMPONENTS, [])

    local_dag_runner.LocalDagRunner().run(self._getTestPipeline())

    self.assertEqual(self.RAN_COMPONENTS, ['Load', 'Train', 'Validate'])

  def testSimplePipelinePartialRun(self):
    self.assertEqual(self.RAN_COMPONENTS, [])

    local_dag_runner.LocalDagRunner().run(
        self._getTestPipeline(),
        run_options=pipeline_py.RunOptions(to_nodes=['Train']))

    self.assertEqual(self.RAN_COMPONENTS, ['Load', 'Train'])

  def testSimplePipelineRunWithIR(self):
    self.assertEqual(self.RAN_COMPONENTS, [])

    local_dag_runner.LocalDagRunner().run_with_ir(self._getTestPipelineIR())

    self.assertEqual(self.RAN_COMPONENTS, ['Load', 'Train', 'Validate'])

  def testSimplePipelinePartialRunWithIR(self):
    self.assertEqual(self.RAN_COMPONENTS, [])

    pr_opts = pipeline_pb2.PartialRun()
    pr_opts.to_nodes.append('Train')
    pr_opts.snapshot_settings.latest_pipeline_run_strategy.SetInParent()
    local_dag_runner.LocalDagRunner().run_with_ir(
        self._getTestPipelineIR(),
        run_options=pipeline_pb2.RunOptions(partial_run=pr_opts))

    self.assertEqual(self.RAN_COMPONENTS, ['Load', 'Train'])

  def testBeamComponentWithPlaceHolderArgs(self):
    # Set env vars for the placeholder
    direct_running_mode_env_var_name = 'DIRECT_RUNNING_MODE'
    direct_running_mode = 'multi_processing'
    direct_num_workers = 2
    num_workers_env_var_name = 'NUM_WORKERS'
    num_workers_env_var_value = f'--direct_num_workers={direct_num_workers}'

    os.environ[direct_running_mode_env_var_name] = direct_running_mode
    os.environ[num_workers_env_var_name] = num_workers_env_var_value

    local_dag_runner.LocalDagRunner().run(self._getTestBeamComponentPipeline(num_workers_env_var_name,
                                                                             direct_running_mode_env_var_name))

    self.assertEqual(self.BEAM_ARG_VALUES['direct_num_workers'], num_workers_env_var_value)
    self.assertEqual(self.BEAM_ARG_VALUES['direct_running_mode'], num_workers_env_var_value)


if __name__ == '__main__':
  absl.testing.absltest.main()
