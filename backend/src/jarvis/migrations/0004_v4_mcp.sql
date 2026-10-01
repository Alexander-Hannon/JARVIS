-- V4 MCP client hub. Servers are added by AJ in the authenticated UI only.
-- A server's tool definitions are pinned by hash at review time; any change disables it until AJ reviews again.
CREATE TABLE mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,                 -- short slug used in tool names: mcp__<name>__<tool>
  transport TEXT NOT NULL CHECK (transport IN ('stdio','http')),
  command TEXT,                              -- stdio: executable
  args TEXT NOT NULL DEFAULT '[]',           -- stdio: JSON list
  url TEXT,                                  -- http: streamable-HTTP endpoint
  secret_ciphertext TEXT,                    -- encrypted JSON {"env": {...}, "bearer": "..."}; never sent to the model
  status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | active | changed | error | disabled
  pinned_hash TEXT,
  discovered_hash TEXT,
  discovered TEXT NOT NULL DEFAULT '[]',     -- JSON [{name, description, input_schema, annotations}]
  tool_modes TEXT NOT NULL DEFAULT '{}',     -- JSON {tool: {"mode": "off"|"read"|"propose", "tier": 1|2}}
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_at TEXT
);
