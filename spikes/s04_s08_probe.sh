#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# S-08 probe (CL-5). Runs inside the container through `podman exec -i s08c sh -s`.
# Prints the inode and type of /run/s08.sock as the container sees it, then the reply
# of whichever server answers on that path.
stat -c 'inode=%i type=%F' /run/s08.sock
printf 'ping\n' | nc -U /run/s08.sock
echo "nc_rc=$?"
