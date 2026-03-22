# fusion-mcp

Open-source MCP server for controlling Autodesk Fusion 360 from any AI agent. just Python.

```
MCP Client ──stdio──▶ MCP Server (Python) ──TCP──▶ Fusion 360 Add-in ──▶ Fusion API
```

## Setup

### 1. Clone and install

**With uv (recommended):**
```bash
git clone https://github.com/prim-design/fusion-mcp.git
cd fusion-mcp
uv pip install -e .
```

**With pip (macOS/Linux):**
```bash
git clone https://github.com/prim-design/fusion-mcp.git
cd fusion-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**With pip (Windows):**
```powershell
git clone https://github.com/prim-design/fusion-mcp.git
cd fusion-mcp
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

Requires Python 3.10+.

### 2. Install the Fusion 360 add-in

Copy the `addin/` folder to Fusion's add-ins directory:

**macOS:**
```bash
cp -r addin/ "$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionMCP"
```

**Windows:**
```powershell
xcopy /E addin "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP\"
```

Then in Fusion 360: **Shift+S** → Add-Ins → select **FusionMCP** → **Run** (optionally check "Run on Startup").

### 3. Connect your MCP client

**Claude Code:**
```bash
claude mcp add fusion-mcp -- /path/to/fusion-mcp/.venv/bin/fusion-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "fusion-mcp": {
      "command": "/path/to/fusion-mcp/.venv/bin/fusion-mcp"
    }
  }
}
```

Works with any MCP client — run `.venv/bin/fusion-mcp` as a stdio server.

## Tools (49)

| Category | Tools |
|----------|-------|
| **Sketch** | `create_sketch` `finish_sketch` `draw_line` `draw_rectangle` `draw_circle` `draw_arc` `draw_polygon` `draw_spline` `draw_slot` |
| **3D Features** | `extrude` `revolve` `sweep` `loft` |
| **Modifications** | `fillet` `chamfer` `shell` `draft` |
| **Patterns** | `pattern_rectangular` `pattern_circular` `mirror` |
| **Booleans** | `combine` `split_body` |
| **Components** | `create_component` `list_components` `delete_component` `move_component` `rotate_component` |
| **Joints** | `create_joint` (all 7 types) `set_joint_limits` `drive_joint` `list_joints` |
| **Rigid Groups** | `create_rigid_group` `list_rigid_groups` `delete_rigid_group` |
| **Inspection** | `get_design_info` `get_body_info` `measure` `check_interference` `fit_view` `screenshot` |
| **Export/Import** | `export_stl` `export_step` `export_3mf` `import_mesh` |
| **Utility** | `undo` `delete_body` `delete_sketch` `batch` |
| **Escape Hatch** | `execute_python` — run arbitrary Python inside Fusion with full API access |

### Joint types

`create_joint` supports all 7 Fusion joint types: **rigid**, **revolute**, **slider**, **cylindrical**, **pin_slot**, **planar**, **ball**. Uses as-built joints by default (components stay in place).

### Screenshot

`screenshot` captures the viewport as a PNG image returned directly to the AI. Supports preset views (`front`, `back`, `top`, `bottom`, `left`, `right`, `iso`) and custom camera positions via `eye_x/y/z` + `target_x/y/z`.

## Important notes

- **Units**: All lengths in **cm**, all angles in **degrees**
- **Z-negation gotcha**: On XZ plane, sketch +Y = world -Z. On YZ plane, sketch +X = world -Z.
- **Thread safety**: The add-in dispatches all API calls to Fusion's main thread via CustomEvent
- **Escape hatch**: `execute_python` runs arbitrary Python inside Fusion for anything not covered by curated tools

## How it works

The MCP server (`server/`) runs as a stdio process your AI agent connects to. It forwards commands over a TCP socket to the Fusion 360 add-in (`addin/`), which runs inside Fusion and executes API calls on the main thread using Fusion's CustomEvent system for thread safety.

## License

MIT
