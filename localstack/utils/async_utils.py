import time
import asyncio
import concurrent.futures
from contextvars import copy_context
from localstack.utils import common
from localstack.utils.common import FuncThread, TMP_THREADS, start_worker_thread

# reference to named event loop instances
EVENT_LOOPS = {}


class AdaptiveThreadPool(concurrent.futures.ThreadPoolExecutor):
    """ Thread pool executor that maintains a maximum of 'core_size' reusable threads in
        the core pool, and creates new thread instances as needed (if the core pool is full). """

    DEFAULT_CORE_POOL_SIZE = 30

    def __init__(self, core_size=None):
        self.core_size = core_size or self.DEFAULT_CORE_POOL_SIZE
        super(AdaptiveThreadPool, self).__init__(max_workers=self.core_size)

    def submit(self, fn, *args, **kwargs):
        # if idle threads are available, don't spin new threads
        if self.has_idle_threads():
            return super(AdaptiveThreadPool, self).submit(fn, *args, **kwargs)

        def _run(*tmpargs):
            return fn(*args, **kwargs)
        thread = start_worker_thread(_run)
        return thread.result_future

    def has_idle_threads(self):
        if hasattr(self, '_idle_semaphore'):
            return self._idle_semaphore.acquire(timeout=0)
        num_threads = len(self._threads)
        return num_threads < self._max_workers


# Thread pool executor for running sync functions in async context.
# Note: For certain APIs like DynamoDB, we need 3x threads for each parallel request,
# as during request processing the API calls out to the DynamoDB API again (recursively).
# (TODO: This could potentially be improved if we move entirely to asyncio functions.)
THREAD_POOL = AdaptiveThreadPool()
TMP_THREADS.append(THREAD_POOL)


class AsyncThread(FuncThread):

    def __init__(self, async_func_gen=None, loop=None):
        """ Pass a function that receives an event loop instance and a shutdown event,
            and returns an async function. """
        FuncThread.__init__(self, self.run_func, None)
        self.async_func_gen = async_func_gen
        self.loop = loop
        self.shutdown_event = None

    def run_func(self, *args):
        loop = self.loop or ensure_event_loop()
        self.shutdown_event = asyncio.Event()
        if self.async_func_gen:
            async_func = self.async_func_gen(loop, self.shutdown_event)
            if async_func:
                loop.run_until_complete(async_func)
        loop.run_forever()

    def stop(self, quiet=None):
        if self.shutdown_event:
            self.shutdown_event.set()
            self.shutdown_event = None

    @classmethod
    def run_async(cls, func=None, loop=None):
        thread = AsyncThread(func, loop=loop)
        thread.start()
        TMP_THREADS.append(thread)
        return thread


async def run_sync(func, *args, thread_pool=None):
    loop = asyncio.get_running_loop()
    thread_pool = thread_pool or THREAD_POOL
    return await loop.run_in_executor(thread_pool, copy_context().run, func, *args)


def ensure_event_loop():
    try:
        return asyncio.get_event_loop()
    except Exception:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def get_named_event_loop(name):
    result = EVENT_LOOPS.get(name)
    if result:
        return result

    def async_func_gen(loop, shutdown_event):
        EVENT_LOOPS[name] = loop

    AsyncThread.run_async(async_func_gen)
    time.sleep(1)
    return EVENT_LOOPS[name]


async def receive_from_queue(queue):
    def get():
        # run in a retry loop (instead of blocking forever) to allow for graceful shutdown
        while True:
            try:
                if common.INFRA_STOPPED:
                    return
                return queue.get(timeout=1)
            except Exception:
                pass

    msg = await run_sync(get)
    return msg
