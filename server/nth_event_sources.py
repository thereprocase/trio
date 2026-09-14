"""Channel transports for Trio's local event service."""
import json
import os
from pathlib import Path

from nth_spoke_monitor import MCPSSEClient


class LocalSource:
    """Use the canonical tools against the local DB; retain capability checks."""
    def connect(self):
        import nth_server
        self.server = nth_server

    def call_tool(self, name, arguments=None, timeout=60):
        operation = name.removeprefix('quartet_').removeprefix('trio_')
        if operation not in ('poll', 'status'):
            raise ValueError('Unsupported event source operation')
        result = getattr(self.server, 'nth_' + operation)(**(arguments or {}))
        if isinstance(result, str):
            return json.loads(result)
        # Image polls are MCP content arrays: the first block is the JSON body.
        if isinstance(result, list) and result:
            block = result[0]
            return json.loads(block.text if hasattr(block, 'text') else block['text'])
        return result

    def close(self):
        pass


def create_source(binding):
    if binding.get('source', 'quartet') == 'local':
        expected = (Path(os.environ.get('NTH_HOME', str(Path.home() / '.claude' / 'nth'))) / 'nth.db').resolve()
        if Path(binding['url']).resolve() != expected:
            raise ValueError('Local binding must use this host service database')
        return LocalSource()
    return MCPSSEClient(binding['url'])
