#
# Copyright oVirt Authors
# SPDX-License-Identifier: GPL-2.0-or-later
#
#

import contextlib
import logging
import os

import ost_utils.network_utils as network_utils

from ost_utils.selenium.grid import CHROME_CONTAINER_IMAGE
from ost_utils.selenium.grid import FIREFOX_CONTAINER_IMAGE
from ost_utils.selenium.grid import FFMPEG_CONTAINER_IMAGE
from ost_utils.selenium.grid import HUB_CONTAINER_IMAGE
from ost_utils.selenium.grid import common
from ost_utils.shell import shell
from ost_utils.shell import ShellError


GRID_STARTUP_RETRIES = 3
HUB_IP = "127.0.0.1"
HUB_PORT = 4444
LOGGER = logging.getLogger(__name__)
NODE_PORT_GEN = iter(range(5600, 5700))
NODE_DISPLAY_ADDR_GEN = iter(range(100, 200))


def _log_issues(pod_name, hub_name, node_names, podman_cmd, videos_names):
    LOGGER.error(
        "Pod inspection: \n%s"
        % shell([podman_cmd, "pod", "inspect", pod_name])
    )
    LOGGER.error("Hub logs: \n%s" % shell([podman_cmd, "logs", hub_name]))
    for name in node_names:
        LOGGER.error(
            "Node %s logs: \n%s" % (name, shell([podman_cmd, "logs", name]))
        )
    for video in videos_names:
        LOGGER.error(
            "Video %s logs: \n%s" % (video, shell([podman_cmd, "logs", video]))
        )


@contextlib.contextmanager
def _pod(hub_port, podman_cmd):
    network_backend = os.getenv('PODMAN_NETWORK_BACKEND')
    if network_backend is None:
        network_backend_options = ["--network=slirp4netns:enable_ipv6=true"]
    else:
        network_backend_options = [
            f"--network={network_backend}:enable_ipv6=true"
        ]
    name = shell(
        [
            podman_cmd,
            "pod",
            "create",
            *network_backend_options,
            "-p",
            f"{hub_port}:{hub_port}",
        ]
    ).strip()
    try:
        yield name
    finally:
        shell([podman_cmd, "pod", "rm", "-f", name])


@contextlib.contextmanager
def _hub(image, hub_port, pod_name, podman_cmd):
    name = shell(
        [
            podman_cmd,
            "run",
            "-d",
            "-e",
            "SE_OPTS=-port {}".format(hub_port),
            "-v",
            "/dev/shm:/dev/shm",
            "--pod",
            pod_name,
            image,
        ]
    ).strip()
    try:
        yield name
    finally:
        shell([podman_cmd, "rm", "-f", name])


# When running multiple containers in a pod, they compete over
# resources like network ports. This is why we can't simply run
# multiple selenium nodes in a single pod - we need to change
# the ports they're using by default and the 'DISPLAY' variable
# (we're using debug images which run VNC server) to some unique
# values.
@contextlib.contextmanager
def _nodes(
    images,
    hub_port,
    pod_name,
    engine_dns_entry,
    podman_cmd,
    ui_artifacts_dir,
):
    nodes_dict = {}

    for image in images:
        display = next(NODE_DISPLAY_ADDR_GEN)
        name = shell(
            [
                podman_cmd,
                "run",
                "-d",
                "-v",
                "/dev/shm:/dev/shm",
                "-v",
                f"{ui_artifacts_dir}:/export:Z",
                "--add-host={}".format(engine_dns_entry),
                "-e",
                "HUB_HOST={}".format(HUB_IP),
                "-e",
                "HUB_PORT={}".format(hub_port),
                "-e",
                "SE_OPTS=-port {}".format(next(NODE_PORT_GEN)),
                "-e",
                "DISPLAY=:{}".format(display),
                "-e",
                "VNC_NO_PASSWORD=1",
                "--pod",
                pod_name,
                image,
            ]
        ).strip()
        nodes_dict.update({image: {'name': name, 'display': display}})

    try:
        yield nodes_dict
    finally:
        log_dir_path = os.path.join(ui_artifacts_dir, 'selenium_grid_nodes')
        os.makedirs(log_dir_path, exist_ok=True)
        for node_dict in nodes_dict.values():
            save_node_logs(log_dir_path, node_dict, podman_cmd)
            shell([podman_cmd, "rm", "-f", node_dict['name']])


def save_node_logs(dir_path, node_dict, podman_cmd):
    file_path = os.path.join(dir_path, node_dict['name'] + '.log')
    with open(file_path, "w", encoding='UTF8') as node_log_file:
        node_log_file.write(shell([podman_cmd, "logs", node_dict['name']]))


@contextlib.contextmanager
def _video_recorders(pod_name, podman_cmd, nodes_dict, ui_artifacts_dir):
    videos = []
    if ui_artifacts_dir is not None:
        for image, values in nodes_dict.items():
            video = shell(
                [
                    podman_cmd,
                    "run",
                    "-d",
                    "-v",
                    f"{ui_artifacts_dir}:/videos:Z",
                    "-e",
                    f"DISPLAY_CONTAINER_NAME={' '}",
                    "-e",
                    f"DISPLAY={values['display']}",
                    "-e",
                    f"FILE_NAME=video-{image.split('/')[-1].split('-')[1]}"
                    f".mp4",
                    "--pod",
                    pod_name,
                    FFMPEG_CONTAINER_IMAGE,
                ]
            ).strip()
            videos.append(video)

    try:
        yield videos
    finally:
        for video in videos:
            shell([podman_cmd, "stop", video])
            shell([podman_cmd, "rm", "-f", video])


@contextlib.contextmanager
def _grid(
    engine_fqdn,
    engine_ip,
    node_images,
    hub_image,
    hub_port,
    podman_cmd,
    ui_artifacts_dir,
):
    if node_images is None:
        node_images = [CHROME_CONTAINER_IMAGE, FIREFOX_CONTAINER_IMAGE]

    engine_dns_entry = "{}:{}".format(engine_fqdn, engine_ip)

    with _pod(hub_port, podman_cmd) as pod_name:
        with _hub(hub_image, hub_port, pod_name, podman_cmd) as hub_name:
            with _nodes(
                node_images,
                hub_port,
                pod_name,
                engine_dns_entry,
                podman_cmd,
                ui_artifacts_dir,
            ) as nodes_dict:
                node_names = [
                    node_dict['name'] for node_dict in nodes_dict.values()
                ]
                with _video_recorders(
                    pod_name, podman_cmd, nodes_dict, ui_artifacts_dir
                ) as videos_names:
                    url = common.GRID_URL_TEMPLATE.format(HUB_IP, hub_port)
                    try:
                        common.grid_health_check(url, len(node_images))
                        yield url
                    except common.SeleniumGridError:
                        _log_issues(
                            pod_name,
                            hub_name,
                            node_names,
                            podman_cmd,
                            videos_names,
                        )
                        raise


@contextlib.contextmanager
def grid(
    engine_fqdn,
    engine_ip,
    node_images=None,
    hub_image=HUB_CONTAINER_IMAGE,
    retries=GRID_STARTUP_RETRIES,
    podman_cmd="podman",
    ui_artifacts_dir=None,
):
    for attempt in range(retries):
        hub_port = network_utils.find_free_port(HUB_PORT, HUB_PORT + 100)
        LOGGER.debug(
            f"Attempt no {attempt} to run the grid on {hub_port} port"
        )
        try:
            with _grid(
                engine_fqdn,
                engine_ip,
                node_images,
                hub_image,
                hub_port,
                podman_cmd,
                ui_artifacts_dir,
            ) as url:
                LOGGER.debug(f"Grid is up: {url}")
                yield url
        except (common.SeleniumGridError, ShellError):
            if attempt < retries - 1:
                LOGGER.warning("Grid startup failed, retrying...")
            else:
                raise
        else:
            break
