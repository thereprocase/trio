#!/usr/bin/env python3
"""Claude's native Monitor entry point for a saved Trio/Quartet identity."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--filter', choices=['all', 'about', 'at'], default='about')
    args = parser.parse_args()
    path = Path(args.identity)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Identity file must be private (chmod 600)')
    identity = json.loads(path.read_text(encoding='utf-8'))
    if identity['source'] == 'local':
        import nth_monitor
        nth_monitor.monitor(identity['channel'], identity['member_id'],
                            filter_mode=args.filter, _db_path=Path(identity['url']),
                            session_token=identity['session_token'])
    else:
        from nth_spoke_monitor import MCPSSEClient, monitor
        client = MCPSSEClient(identity['url'])
        try:
            client.connect()
            monitor(client, identity['channel'], identity['member_id'], args.filter,
                    identity['session_token'], 15, 30)
        finally:
            client.close()


if __name__ == '__main__':
    main()
