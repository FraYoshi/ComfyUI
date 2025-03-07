import asyncio
import itertools
import os
import shutil
import threading

from comfy.cli_args import args

if os.name == "nt":
    import logging
    logging.getLogger("xformers").addFilter(lambda record: 'A matching Triton is not available' not in record.getMessage())

if __name__ == "__main__":
    if args.dont_upcast_attention:
        print("disabling upcasting of attention")
        os.environ['ATTN_PRECISION'] = "fp16"

    if args.cuda_device is not None:
        os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda_device)
        print("Set cuda device to:", args.cuda_device)


import yaml

import execution
import folder_paths
import server
from nodes import init_custom_nodes


def prompt_worker(q, server):
    e = execution.PromptExecutor(server)
    while True:
        item, item_id = q.get()
        e.execute(item[-2], item[-1])
        q.task_done(item_id, e.outputs)

async def run(server, address='', port=8188, verbose=True, call_on_start=None):
    await asyncio.gather(server.start(address, port, verbose, call_on_start), server.publish_loop())

def hijack_progress(server):
    from tqdm.auto import tqdm
    orig_func = getattr(tqdm, "update")
    def wrapped_func(*args, **kwargs):
        pbar = args[0]
        v = orig_func(*args, **kwargs)
        server.send_sync("progress", { "value": pbar.n, "max": pbar.total}, server.client_id)            
        return v
    setattr(tqdm, "update", wrapped_func)

def cleanup_temp():
    temp_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "temp")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)

def load_extra_path_config(yaml_path):
    with open(yaml_path, 'r') as stream:
        config = yaml.safe_load(stream)
    for c in config:
        conf = config[c]
        if conf is None:
            continue
        base_path = None
        if "base_path" in conf:
            base_path = conf.pop("base_path")
        for x in conf:
            for y in conf[x].split("\n"):
                if len(y) == 0:
                    continue
                full_path = y
                if base_path is not None:
                    full_path = os.path.join(base_path, full_path)
                print("Adding extra search path", x, full_path)
                folder_paths.add_model_folder_path(x, full_path)

if __name__ == "__main__":
    cleanup_temp()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = server.PromptServer(loop)
    q = execution.PromptQueue(server)

    init_custom_nodes()
    server.add_routes()
    hijack_progress(server)

    threading.Thread(target=prompt_worker, daemon=True, args=(q,server,)).start()

    address = args.listen

    dont_print = args.dont_print_server

    extra_model_paths_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "extra_model_paths.yaml")
    if os.path.isfile(extra_model_paths_config_path):
        load_extra_path_config(extra_model_paths_config_path)

    if args.extra_model_paths_config:
        for config_path in itertools.chain(*args.extra_model_paths_config):
            load_extra_path_config(config_path)

    if args.output_directory:
        output_dir = os.path.abspath(args.output_directory)
        print(f"Setting output directory to: {output_dir}")
        folder_paths.set_output_directory(output_dir)

    port = args.port

    if args.quick_test_for_ci:
        exit(0)

    call_on_start = None
    if args.windows_standalone_build:
        def startup_server(address, port):
            import webbrowser
            webbrowser.open("http://{}:{}".format(address, port))
        call_on_start = startup_server

    if os.name == "nt":
        try:
            loop.run_until_complete(run(server, address=address, port=port, verbose=not dont_print, call_on_start=call_on_start))
        except KeyboardInterrupt:
            pass
    else:
        loop.run_until_complete(run(server, address=address, port=port, verbose=not dont_print, call_on_start=call_on_start))

    cleanup_temp()
