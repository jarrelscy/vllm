import asyncio
import copy
from collections import defaultdict
import os
import pickle
import importlib
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from vllm.executor.multi_gpu_executor import (MultiGPUExecutor,
                                              MultiGPUExecutorAsync)
from vllm.engine.ray_utils import RayWorkerVllm, ray
from vllm.logger import init_logger
from vllm.sequence import SamplerOutput, SequenceGroupMetadata
from vllm.utils import (set_cuda_visible_devices, get_ip, get_open_port,
                        get_distributed_init_method, make_async)

if ray is not None:
    from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

if TYPE_CHECKING:
    from ray.util.placement_group import PlacementGroup

logger = init_logger(__name__)

# A map between the device type (in device config) to its worker module.
DEVICE_TO_WORKER_MODULE_MAP = {
    "cuda": "vllm.worker.worker",
    "neuron": "vllm.worker.neuron_worker",
}

# If the env var is set, it uses the Ray's compiled DAG API
# which optimizes the control plane overhead.
# Run vLLM with VLLM_USE_RAY_COMPILED_DAG=1 to enable it.
USE_RAY_COMPILED_DAG = bool(os.getenv("VLLM_USE_RAY_COMPILED_DAG", 0))


class RayGPUExecutor(MultiGPUExecutor):

    def _init_executor(self) -> None:

        assert self.parallel_config.worker_use_ray
        placement_group = self.parallel_config.placement_group

        # Disable Ray usage stats collection.
        ray_usage = os.environ.get("RAY_USAGE_STATS_ENABLED", "0")
        if ray_usage != "1":
            os.environ["RAY_USAGE_STATS_ENABLED"] = "0"

        # Create the parallel GPU workers.
        self._init_workers_ray(placement_group)

        # Profile the memory usage and initialize the cache.
        self._init_cache()

        self.forward_dag = None
        if USE_RAY_COMPILED_DAG:
            self.forward_dag = self._compiled_ray_dag()

    def _dispatch_worker(self):
        worker_module = DEVICE_TO_WORKER_MODULE_MAP[
            self.device_config.device_type]
        imported_worker = importlib.import_module(worker_module)
        Worker = imported_worker.Worker
        return Worker

    def _init_workers_ray(self, placement_group: "PlacementGroup",
                          **ray_remote_kwargs):
        if self.parallel_config.tensor_parallel_size == 1:
            # For single GPU case, we use a ray worker with constrained memory.
            num_gpus = self.cache_config.gpu_memory_utilization
        else:
            # Otherwise, the ray workers are allocated with a full GPU.
            num_gpus = 1

        # The driver dummy worker does not actually use any resources.
        # It holds the resource for the driver worker.
        self.driver_dummy_worker: RayWorkerVllm = None
        # The remaining workers are the actual ray actors.
        self.workers: List[RayWorkerVllm] = []

        # Create the workers.
        driver_ip = get_ip()
        for bundle_id, bundle in enumerate(placement_group.bundle_specs):
            if not bundle.get("GPU", 0):
                continue
            scheduling_strategy = PlacementGroupSchedulingStrategy(
                placement_group=placement_group,
                placement_group_capture_child_tasks=True,
                placement_group_bundle_index=bundle_id,
            )
            worker = ray.remote(
                num_cpus=0,
                num_gpus=num_gpus,
                scheduling_strategy=scheduling_strategy,
                **ray_remote_kwargs,
            )(RayWorkerVllm).remote(self.model_config.trust_remote_code)

            worker_ip = ray.get(worker.get_node_ip.remote())
            if worker_ip == driver_ip and self.driver_dummy_worker is None:
                # If the worker is on the same node as the driver, we use it
                # as the resource holder for the driver process.
                self.driver_dummy_worker = worker
            else:
                # Else, added to the list of workers.
                self.workers.append(worker)

        if self.driver_dummy_worker is None:
            raise ValueError(
                "Ray does not allocate any GPUs on the driver node. Consider "
                "adjusting the Ray placement group or running the driver on a "
                "GPU node.")

        # Get the set of GPU IDs used on each node.
        driver_node_id, driver_gpu_ids = ray.get(
            self.driver_dummy_worker.get_node_and_gpu_ids.remote())
        worker_node_and_gpu_ids = ray.get(
            [worker.get_node_and_gpu_ids.remote() for worker in self.workers])

        node_workers = defaultdict(list)
        node_gpus = defaultdict(list)

        node_workers[driver_node_id].append(0)
        node_gpus[driver_node_id].extend(driver_gpu_ids)
        for i, (node_id, gpu_ids) in enumerate(worker_node_and_gpu_ids,
                                               start=1):
            node_workers[node_id].append(i)
            node_gpus[node_id].extend(gpu_ids)
        for node_id, gpu_ids in node_gpus.items():
            node_gpus[node_id] = sorted(gpu_ids)

        # Set CUDA_VISIBLE_DEVICES for the driver and workers.
        set_cuda_visible_devices(node_gpus[driver_node_id])
        for worker, (node_id, _) in zip(self.workers, worker_node_and_gpu_ids):
            worker.set_cuda_visible_devices.remote(node_gpus[node_id])

        distributed_init_method = get_distributed_init_method(
            driver_ip, get_open_port())

        # Lazy import the Worker to avoid importing torch.cuda/xformers
        # before CUDA_VISIBLE_DEVICES is set in the Worker
        Worker = self._dispatch_worker()

        model_config = copy.deepcopy(self.model_config)
        parallel_config = copy.deepcopy(self.parallel_config)
        scheduler_config = copy.deepcopy(self.scheduler_config)
        device_config = copy.deepcopy(self.device_config)
        lora_config = copy.deepcopy(self.lora_config)
        kv_cache_dtype = self.cache_config.cache_dtype

        # Initialize the actual workers with the Worker class.
        for rank, (worker, (node_id, _)) in enumerate(
                zip(self.workers, worker_node_and_gpu_ids),
                start=1,
        ):
            local_rank = node_workers[node_id].index(rank)
            worker.init_worker.remote(
                lambda rank=rank, local_rank=local_rank: Worker(
                    model_config,
                    parallel_config,
                    scheduler_config,
                    device_config,
                    local_rank,
                    rank,
                    distributed_init_method,
                    lora_config=lora_config,
                    kv_cache_dtype=kv_cache_dtype,
                ))

        driver_rank = 0
        driver_local_rank = node_workers[driver_node_id].index(driver_rank)
        self._init_driver_worker_and_model(driver_rank, driver_local_rank,
                                           distributed_init_method)

    def execute_model(self,
                      seq_group_metadata_list: List[SequenceGroupMetadata],
                      blocks_to_swap_in: Dict[int, int],
                      blocks_to_swap_out: Dict[int, int],
                      blocks_to_copy: Dict[int, List[int]]) -> SamplerOutput:
        all_outputs = self._run_workers(
            "execute_model",
            driver_kwargs={
                "seq_group_metadata_list": seq_group_metadata_list,
                "blocks_to_swap_in": blocks_to_swap_in,
                "blocks_to_swap_out": blocks_to_swap_out,
                "blocks_to_copy": blocks_to_copy,
            },
            use_ray_compiled_dag=USE_RAY_COMPILED_DAG)

        # Only the driver worker returns the sampling results.
        output = all_outputs[0]
        return output

    def _run_workers(
        self,
        method: str,
        *args,
        driver_args: Optional[List[Any]] = None,
        driver_kwargs: Optional[Dict[str, Any]] = None,
        max_concurrent_workers: Optional[int] = None,
        use_ray_compiled_dag: bool = False,
        **kwargs,
    ) -> Any:
        """Runs the given method on all workers."""

        if max_concurrent_workers:
            raise NotImplementedError(
                "max_concurrent_workers is not supported yet.")

        if use_ray_compiled_dag:
            # Right now, compiled DAG can only accept a single
            # input. TODO(sang): Fix it.
            output_channels = self.forward_dag.execute(1)
        else:
            # Start the ray workers first.
            ray_worker_outputs = [
                worker.execute_method.remote(method, *args, **kwargs)
                for worker in self.workers
            ]

        if driver_args is None:
            driver_args = args
        if driver_kwargs is None:
            driver_kwargs = kwargs

        # Start the driver worker after all the ray workers.
        driver_worker_output = getattr(self.driver_worker,
                                       method)(*driver_args, **driver_kwargs)

        # Get the results of the ray workers.
        if use_ray_compiled_dag:
            try:
                ray_worker_outputs = [
                    pickle.loads(chan.begin_read()) for chan in output_channels
                ]
            finally:
                # Has to call end_read in order to reuse the DAG.
                for chan in output_channels:
                    chan.end_read()
        else:
            ray_worker_outputs = ray.get(ray_worker_outputs)

        return [driver_worker_output] + ray_worker_outputs

    def _compiled_ray_dag(self):
        import pkg_resources
        required_version = "2.9"
        current_version = pkg_resources.get_distribution("ray").version
        if current_version < required_version:
            raise ValueError(f"Ray version {required_version} or greater is "
                             f"required, but found {current_version}")

        from ray.dag import MultiOutputNode, InputNode
        assert self.parallel_config.worker_use_ray

        # Right now, compiled DAG requires at least 1 arg. We send
        # a dummy value for now. It will be fixed soon.
        with InputNode() as input_data:
            forward_dag = MultiOutputNode([
                worker.execute_model_compiled_dag_remote.bind(input_data)
                for worker in self.workers
            ])
        return forward_dag.experimental_compile()

    def check_health(self) -> None:
        """Raises an error if engine is unhealthy."""
        self._check_if_any_actor_is_dead()

    def _check_if_any_actor_is_dead(self):
        if not self.workers:
            return

        dead_actors = []
        for actor in self.workers:
            actor_state = ray.state.actors(actor._ray_actor_id.hex())  # pylint: disable=protected-access
            if actor_state["State"] == "DEAD":
                dead_actors.append(actor)
        if dead_actors:
            raise RuntimeError("At least one Worker is dead. "
                               f"Dead Workers: {dead_actors}. ")


class RayGPUExecutorAsync(RayGPUExecutor, MultiGPUExecutorAsync):

    async def _run_workers_async(
        self,
        method: str,
        *args,
        driver_args: Optional[List[Any]] = None,
        driver_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Any:
        """Runs the given method on all workers."""
        coros = []

        if driver_args is None:
            driver_args = args
        if driver_kwargs is None:
            driver_kwargs = kwargs

        # Run the driver worker asynchronously.
        driver_executor = make_async(getattr(self.driver_worker, method))
        coros.append(driver_executor(*driver_args, **driver_kwargs))

        # Run the ray workers asynchronously.
        for worker in self.workers:
            coros.append(worker.execute_method.remote(method, *args, **kwargs))

        all_outputs = await asyncio.gather(*coros)
        return all_outputs
