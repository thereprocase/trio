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


def select_messages(poll, filter_mode):
    # Filter the newly returned message itself, not stale batch-level flags.
    # Fetch all visible messages so @someone-else plus !me cannot be filtered
    # out by the hub's mentions_only shortcut before its bang reaches us.
    # Lives here, not in the Codex relay, so a Claude frontend can filter
    # without importing the Codex socket client and its optional dependency.
    return [message for message in poll.get('messages', [])
            if filter_mode == 'all' or message.get('banged')
            or message.get('mentioned')
            or (filter_mode == 'about' and message.get('referenced'))]


def create_source(binding):
    if binding.get('source', 'quartet') == 'local':
        expected = (Path(os.environ.get('NTH_HOME', str(Path.home() / '.claude' / 'nth'))) / 'nth.db').resolve()
        if Path(binding['url']).resolve() != expected:
            raise ValueError('Local binding must use this host service database')
        return LocalSource()
    return MCPSSEClient(binding['url'])
