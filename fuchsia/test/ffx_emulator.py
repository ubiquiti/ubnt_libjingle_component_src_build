# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Provide helpers for running Fuchsia's `ffx emu`."""

import argparse
import ast
import logging
import os
import json
import random
import subprocess

from contextlib import AbstractContextManager

from common import check_ssh_config_file, run_ffx_command, \
                   SDK_ROOT
from compatible_utils import get_host_arch
from ffx_integration import ScopedFfxConfig

_EMU_COMMAND_RETRIES = 3


class FfxEmulator(AbstractContextManager):
    """A helper for managing emulators."""
    def __init__(self, args: argparse.Namespace) -> None:
        if args.product_bundle:
            self._product_bundle = args.product_bundle
        else:
            target_cpu = get_host_arch()
            self._product_bundle = f'terminal.qemu-{target_cpu}'

        self._enable_graphics = args.enable_graphics
        self._hardware_gpu = args.hardware_gpu
        self._logs_dir = args.logs_dir
        self._with_network = args.with_network
        self._node_name = 'fuchsia-emulator-' + str(random.randint(1, 9999))

        # Set the download path parallel to Fuchsia SDK directory
        # permanently so that scripts can always find the product bundles.
        run_ffx_command(('config', 'set', 'pbms.storage.path',
                         os.path.join(SDK_ROOT, os.pardir, 'images')))

        override_file = os.path.join(os.path.dirname(__file__), os.pardir,
                                     'sdk_override.txt')
        self._scoped_pb_metadata = None
        if os.path.exists(override_file):
            with open(override_file) as f:
                pb_metadata = f.read().split('\n')
                pb_metadata.append('{sdk.root}/*.json')
                self._scoped_pb_metadata = ScopedFfxConfig(
                    'pbms.metadata', json.dumps((pb_metadata)))

    def __enter__(self) -> str:
        """Start the emulator.

        Returns:
            The node name of the emulator.
        """

        logging.info('Starting emulator %s', self._node_name)
        if self._scoped_pb_metadata:
            self._scoped_pb_metadata.__enter__()
        check_ssh_config_file()
        emu_command = [
            'emu', 'start', self._product_bundle, '--name', self._node_name
        ]
        if not self._enable_graphics:
            emu_command.append('-H')
        if self._hardware_gpu:
            emu_command.append('--gpu')
        if self._logs_dir:
            emu_command.extend(
                ('-l', os.path.join(self._logs_dir, 'emulator_log')))
        if self._with_network:
            emu_command.extend(('--net', 'tap'))

        # TODO(https://crbug.com/1336776): remove when ffx has native support
        # for starting emulator on arm64 host.
        if get_host_arch() == 'arm64':

            arm64_qemu_dir = os.path.join(SDK_ROOT, 'tools', 'arm64',
                                          'qemu_internal')

            # The arm64 emulator binaries are downloaded separately, so add
            # a symlink to the expected location inside the SDK.
            if not os.path.isdir(arm64_qemu_dir):
                os.symlink(
                    os.path.join(SDK_ROOT, '..', '..', 'qemu-linux-arm64'),
                    arm64_qemu_dir)

            # Add the arm64 emulator binaries to the SDK's manifest.json file.
            sdk_manifest = os.path.join(SDK_ROOT, 'meta', 'manifest.json')
            with open(sdk_manifest, 'r+') as f:
                data = json.load(f)
                for part in data['parts']:
                    if part['meta'] == 'tools/x64/qemu_internal-meta.json':
                        part['meta'] = 'tools/arm64/qemu_internal-meta.json'
                        break
                f.seek(0)
                json.dump(data, f)
                f.truncate()

            # Generate a meta file for the arm64 emulator binaries using its
            # x64 counterpart.
            qemu_arm64_meta_file = os.path.join(SDK_ROOT, 'tools', 'arm64',
                                                'qemu_internal-meta.json')
            qemu_x64_meta_file = os.path.join(SDK_ROOT, 'tools', 'x64',
                                              'qemu_internal-meta.json')
            with open(qemu_x64_meta_file) as f:
                data = str(json.load(f))
            qemu_arm64_meta = data.replace(r'tools/x64', 'tools/arm64')
            with open(qemu_arm64_meta_file, "w+") as f:
                json.dump(ast.literal_eval(qemu_arm64_meta), f)
            emu_command.extend(['--engine', 'qemu'])

        with ScopedFfxConfig('emu.start.timeout', '90'):
            for _ in range(_EMU_COMMAND_RETRIES):

                # If the ffx daemon fails to establish a connection with
                # the emulator after 85 seconds, that means the emulator
                # failed to be brought up and a retry is needed.
                # TODO(fxb/103540): Remove retry when start up issue is fixed.
                try:
                    run_ffx_command(emu_command, timeout=85)
                    break
                except (subprocess.TimeoutExpired,
                        subprocess.CalledProcessError):
                    run_ffx_command(('emu', 'stop'))
        return self._node_name

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        """Shutdown the emulator."""

        logging.info('Stopping the emulator %s', self._node_name)
        # The emulator might have shut down unexpectedly, so this command
        # might fail.
        run_ffx_command(('emu', 'stop', self._node_name), check=False)

        if self._scoped_pb_metadata:
            self._scoped_pb_metadata.__exit__(exc_type, exc_value, traceback)

        # Do not suppress exceptions.
        return False
