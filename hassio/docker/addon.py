"""Init file for Hass.io add-on Docker object."""
from __future__ import annotations

from contextlib import suppress
from ipaddress import IPv4Address, ip_address
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Union, Awaitable

import docker
import requests

from ..addons.build import AddonBuild
from ..const import (
    ENV_TIME,
    ENV_TOKEN,
    MAP_ADDONS,
    MAP_BACKUP,
    MAP_CONFIG,
    MAP_SHARE,
    MAP_SSL,
    SECURITY_DISABLE,
    SECURITY_PROFILE,
)
from ..coresys import CoreSys
from ..exceptions import DockerAPIError
from ..utils import process_lock
from .interface import DockerInterface

if TYPE_CHECKING:
    from ..addons.addon import Addon


_LOGGER: logging.Logger = logging.getLogger(__name__)

AUDIO_DEVICE = "/dev/snd:/dev/snd:rwm"
NO_ADDDRESS = ip_address("0.0.0.0")


class DockerAddon(DockerInterface):
    """Docker Hass.io wrapper for Home Assistant."""

    def __init__(self, coresys: CoreSys, addon: Addon):
        """Initialize Docker Home Assistant wrapper."""
        super().__init__(coresys)
        self.addon = addon

    @property
    def image(self) -> str:
        """Return name of Docker image."""
        return self.addon.image

    @property
    def ip_address(self) -> IPv4Address:
        """Return IP address of this container."""
        if self.addon.host_network:
            return self.sys_docker.network.gateway

        # Extract IP-Address
        try:
            return ip_address(
                self._meta["NetworkSettings"]["Networks"]["hassio"]["IPAddress"]
            )
        except (KeyError, TypeError, ValueError):
            return NO_ADDDRESS

    @property
    def timeout(self) -> int:
        """Return timeout for Docker actions."""
        return self.addon.timeout

    @property
    def version(self) -> str:
        """Return version of Docker image."""
        if self.addon.legacy:
            return self.addon.version
        return super().version

    @property
    def arch(self) -> str:
        """Return arch of Docker image."""
        if self.addon.legacy:
            return self.sys_arch.default
        return super().arch

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return f"addon_{self.addon.slug}"

    @property
    def ipc(self) -> Optional[str]:
        """Return the IPC namespace."""
        if self.addon.host_ipc:
            return "host"
        return None

    @property
    def full_access(self) -> bool:
        """Return True if full access is enabled."""
        return not self.addon.protected and self.addon.with_full_access

    @property
    def environment(self) -> Dict[str, str]:
        """Return environment for Docker add-on."""
        addon_env = self.addon.environment or {}

        # Provide options for legacy add-ons
        if self.addon.legacy:
            for key, value in self.addon.options.items():
                if isinstance(value, (int, str)):
                    addon_env[key] = value
                else:
                    _LOGGER.warning("Can not set nested option %s as Docker env", key)

        return {
            **addon_env,
            ENV_TIME: self.sys_timezone,
            ENV_TOKEN: self.addon.hassio_token,
        }

    @property
    def devices(self) -> List[str]:
        """Return needed devices."""
        devices = []

        # Extend add-on config
        if self.addon.devices:
            devices.extend(self.addon.devices)

        # Use audio devices
        if self.addon.with_audio and self.sys_hardware.support_audio:
            devices.append(AUDIO_DEVICE)

        # Auto mapping UART devices
        if self.addon.auto_uart:
            if self.addon.with_udev:
                serial_devs = self.sys_hardware.serial_devices
            else:
                serial_devs = (
                    self.sys_hardware.serial_devices | self.sys_hardware.serial_by_id
                )

            for device in serial_devs:
                devices.append(f"{device}:{device}:rwm")

        # Return None if no devices is present
        return devices or None

    @property
    def ports(self) -> Optional[Dict[str, Union[str, int, None]]]:
        """Filter None from add-on ports."""
        if self.addon.host_network or not self.addon.ports:
            return None

        return {
            container_port: host_port
            for container_port, host_port in self.addon.ports.items()
            if host_port
        }

    @property
    def security_opt(self) -> List[str]:
        """Controlling security options."""
        security = []

        # AppArmor
        apparmor = self.sys_host.apparmor.available
        if not apparmor or self.addon.apparmor == SECURITY_DISABLE:
            security.append("apparmor:unconfined")
        elif self.addon.apparmor == SECURITY_PROFILE:
            security.append(f"apparmor={self.addon.slug}")

        # Disable Seccomp / We don't support it official and it
        # make troubles on some kind of host systems.
        security.append("seccomp=unconfined")

        return security

    @property
    def tmpfs(self) -> Optional[Dict[str, str]]:
        """Return tmpfs for Docker add-on."""
        options = self.addon.tmpfs
        if options:
            return {"/tmpfs": f"{options}"}
        return None

    @property
    def network_mapping(self) -> Dict[str, str]:
        """Return hosts mapping."""
        return {"hassio": self.sys_docker.network.supervisor}

    @property
    def network_mode(self) -> Optional[str]:
        """Return network mode for add-on."""
        if self.addon.host_network:
            return "host"
        return None

    @property
    def pid_mode(self) -> Optional[str]:
        """Return PID mode for add-on."""
        if not self.addon.protected and self.addon.host_pid:
            return "host"
        return None

    @property
    def volumes(self) -> Dict[str, Dict[str, str]]:
        """Generate volumes for mappings."""
        volumes = {str(self.addon.path_extern_data): {"bind": "/data", "mode": "rw"}}

        addon_mapping = self.addon.map_volumes

        # setup config mappings
        if MAP_CONFIG in addon_mapping:
            volumes.update(
                {
                    str(self.sys_config.path_extern_homeassistant): {
                        "bind": "/config",
                        "mode": addon_mapping[MAP_CONFIG],
                    }
                }
            )

        if MAP_SSL in addon_mapping:
            volumes.update(
                {
                    str(self.sys_config.path_extern_ssl): {
                        "bind": "/ssl",
                        "mode": addon_mapping[MAP_SSL],
                    }
                }
            )

        if MAP_ADDONS in addon_mapping:
            volumes.update(
                {
                    str(self.sys_config.path_extern_addons_local): {
                        "bind": "/addons",
                        "mode": addon_mapping[MAP_ADDONS],
                    }
                }
            )

        if MAP_BACKUP in addon_mapping:
            volumes.update(
                {
                    str(self.sys_config.path_extern_backup): {
                        "bind": "/backup",
                        "mode": addon_mapping[MAP_BACKUP],
                    }
                }
            )

        if MAP_SHARE in addon_mapping:
            volumes.update(
                {
                    str(self.sys_config.path_extern_share): {
                        "bind": "/share",
                        "mode": addon_mapping[MAP_SHARE],
                    }
                }
            )

        # Init other hardware mappings

        # GPIO support
        if self.addon.with_gpio and self.sys_hardware.support_gpio:
            for gpio_path in ("/sys/class/gpio", "/sys/devices/platform/soc"):
                volumes.update({gpio_path: {"bind": gpio_path, "mode": "rw"}})

        # DeviceTree support
        if self.addon.with_devicetree:
            volumes.update(
                {
                    "/sys/firmware/devicetree/base": {
                        "bind": "/device-tree",
                        "mode": "ro",
                    }
                }
            )

        # Kernel Modules support
        if self.addon.with_kernel_modules:
            volumes.update({"/lib/modules": {"bind": "/lib/modules", "mode": "ro"}})

        # Docker API support
        if not self.addon.protected and self.addon.access_docker_api:
            volumes.update(
                {"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"}}
            )

        # Host D-Bus system
        if self.addon.host_dbus:
            volumes.update({"/var/run/dbus": {"bind": "/var/run/dbus", "mode": "rw"}})

        # ALSA configuration
        if self.addon.with_audio:
            volumes.update(
                {
                    str(self.addon.path_extern_asound): {
                        "bind": "/etc/asound.conf",
                        "mode": "ro",
                    }
                }
            )

        return volumes

    def _run(self) -> None:
        """Run Docker image.

        Need run inside executor.
        """
        if self._is_running():
            return

        # Security check
        if not self.addon.protected:
            _LOGGER.warning("%s run with disabled protected mode!", self.addon.name)

        # Cleanup
        with suppress(DockerAPIError):
            self._stop()

        # Create & Run container
        docker_container = self.sys_docker.run(
            self.image,
            version=self.addon.version,
            name=self.name,
            hostname=self.addon.hostname,
            detach=True,
            init=True,
            privileged=self.full_access,
            ipc_mode=self.ipc,
            stdin_open=self.addon.with_stdin,
            network_mode=self.network_mode,
            pid_mode=self.pid_mode,
            ports=self.ports,
            extra_hosts=self.network_mapping,
            devices=self.devices,
            cap_add=self.addon.privileged,
            security_opt=self.security_opt,
            environment=self.environment,
            volumes=self.volumes,
            tmpfs=self.tmpfs,
        )

        self._meta = docker_container.attrs
        _LOGGER.info("Start Docker add-on %s with version %s", self.image, self.version)

        # Write data to DNS server
        self.sys_dns.add_host(ipv4=self.ip_address, names=[self.addon.hostname])

    def _install(
        self, tag: str, image: Optional[str] = None, latest: bool = False
    ) -> None:
        """Pull Docker image or build it.

        Need run inside executor.
        """
        if self.addon.need_build:
            self._build(tag)
        else:
            super()._install(tag, image, latest)

    def _build(self, tag: str) -> None:
        """Build a Docker container.

        Need run inside executor.
        """
        build_env = AddonBuild(self.coresys, self.addon)

        _LOGGER.info("Start build %s:%s", self.image, tag)
        try:
            image, log = self.sys_docker.images.build(
                use_config_proxy=False, **build_env.get_docker_args(tag)
            )

            _LOGGER.debug("Build %s:%s done: %s", self.image, tag, log)

            # Update meta data
            self._meta = image.attrs

        except docker.errors.DockerException as err:
            _LOGGER.error("Can't build %s:%s: %s", self.image, tag, err)
            raise DockerAPIError() from None

        _LOGGER.info("Build %s:%s done", self.image, tag)

    @process_lock
    def export_image(self, tar_file: Path) -> Awaitable[None]:
        """Export current images into a tar file."""
        return self.sys_run_in_executor(self._export_image, tar_file)

    def _export_image(self, tar_file: Path) -> None:
        """Export current images into a tar file.

        Need run inside executor.
        """
        try:
            image = self.sys_docker.api.get_image(f"{self.image}:{self.version}")
        except docker.errors.DockerException as err:
            _LOGGER.error("Can't fetch image %s: %s", self.image, err)
            raise DockerAPIError() from None

        _LOGGER.info("Export image %s to %s", self.image, tar_file)
        try:
            with tar_file.open("wb") as write_tar:
                for chunk in image:
                    write_tar.write(chunk)
        except (OSError, requests.exceptions.ReadTimeout) as err:
            _LOGGER.error("Can't write tar file %s: %s", tar_file, err)
            raise DockerAPIError() from None

        _LOGGER.info("Export image %s done", self.image)

    @process_lock
    def import_image(self, tar_file: Path) -> Awaitable[None]:
        """Import a tar file as image."""
        return self.sys_run_in_executor(self._import_image, tar_file)

    def _import_image(self, tar_file: Path) -> None:
        """Import a tar file as image.

        Need run inside executor.
        """
        try:
            with tar_file.open("rb") as read_tar:
                self.sys_docker.api.load_image(read_tar, quiet=True)

            docker_image = self.sys_docker.images.get(f"{self.image}:{self.version}")
        except (docker.errors.DockerException, OSError) as err:
            _LOGGER.error("Can't import image %s: %s", self.image, err)
            raise DockerAPIError() from None

        self._meta = docker_image.attrs
        _LOGGER.info("Import image %s and version %s", tar_file, self.version)

        with suppress(DockerAPIError):
            self._cleanup()

    @process_lock
    def write_stdin(self, data: bytes) -> Awaitable[None]:
        """Write to add-on stdin."""
        return self.sys_run_in_executor(self._write_stdin, data)

    def _write_stdin(self, data: bytes) -> None:
        """Write to add-on stdin.

        Need run inside executor.
        """
        if not self._is_running():
            raise DockerAPIError() from None

        try:
            # Load needed docker objects
            container = self.sys_docker.containers.get(self.name)
            socket = container.attach_socket(params={"stdin": 1, "stream": 1})
        except docker.errors.DockerException as err:
            _LOGGER.error("Can't attach to %s stdin: %s", self.name, err)
            raise DockerAPIError() from None

        try:
            # Write to stdin
            data += b"\n"
            os.write(socket.fileno(), data)
            socket.close()
        except OSError as err:
            _LOGGER.error("Can't write to %s stdin: %s", self.name, err)
            raise DockerAPIError() from None

    def _stop(self, remove_container=True) -> None:
        """Stop/remove Docker container.

        Need run inside executor.
        """
        if self.ip_address != NO_ADDDRESS:
            self.sys_dns.delete_host(self.addon.hostname)
        super()._stop(remove_container)
