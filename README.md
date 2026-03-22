# fusion-mcp

Open-source MCP server for controlling Autodesk Fusion 360 from any AI agent or MCP client. No closed-source binaries, no middleman processes — just Python.

## Architecture

```
MCP Client (any AI agent) ──stdio──▶ MCP Server (Python)
                                      │
                                 TCP localhost:52361
                                      │
                                      ▼
                            Fusion 360 Add-in
                                      │
                               CustomEvent dispatch
                                      │
                                      ▼
                                Main thread → Fusion API
```

Two components:
1. **MCP Server** (`server/`) — FastMCP server with 40+ curated tools, runs as a standard MCP stdio server
2. **Fusion Add-in** (`addin/`) — TCP socket server inside Fusion 360, executes commands on the main thread

## Tools

| Category | Tools |
|----------|-------|
| **Sketch** | `create_sketch`, `finish_sketch`, `draw_line`, `draw_rectangle`, `draw_circle`, `draw_arc`, `draw_polygon`, `draw_spline`, `draw_slot` |
| **3D Features** | `extrude`, `revolve`, `sweep`, `loft` |
| **Modifications** | `fillet`, `chamfer`, `shell`, `draft` |
| **Patterns** | `pattern_rectangular`, `pattern_circular`, `mirror` |
| **Booleans** | `combine`, `split_body` |
| **Components** | `create_component`, `list_components`, `delete_component`, `move_component`, `rotate_component` |
| **Joints** | `create_joint` (rigid/revolute/slider/cylindrical/pin_slot/planar/ball), `set_joint_limits`, `drive_joint`, `list_joints` |
| **Rigid Groups** | `create_rigid_group`, `list_rigid_groups`, `delete_rigid_group` |
| **Inspection** | `get_design_info`, `get_body_info`, `measure`, `check_interference`, `fit_view` |
| **Export/Import** | `export_stl`, `export_step`, `export_3mf`, `import_mesh` |
| **Utility** | `undo`, `delete_body`, `delete_sketch`, `batch` |
| **Escape Hatch** | `execute_python` — run arbitrary Python inside Fusion with full API access |

## Setup

### 1. Install the MCP server

```bash
cd /path/to/fusion-mcp
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Install the Fusion 360 add-in

Copy the `addin/` folder to Fusion's add-ins directory:

**macOS:**
```bash
cp -r addin/ ~/Library/Application\ Support/Autodesk/Autodesk\ Fusion\ 360/API/AddIns/FusionMCP
```

**Windows:**
```
copy addin %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP
```

Then in Fusion 360:
1. Press `Shift+S` to open Scripts and Add-Ins
2. Go to the **Add-Ins** tab
3. Find **FusionMCP** under "My Add-Ins"
4. Click **Run** (check "Run on Startup" for auto-start)

### 3. Configure your MCP client

**Claude Code:**
```bash
claude mcp add fusion-mcp -- /path/to/fusion-mcp/.venv/bin/fusion-mcp
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "fusion-mcp": {
      "command": "/path/to/fusion-mcp/.venv/bin/fusion-mcp"
    }
  }
}
```

**Any MCP client:** Run the `fusion-mcp` binary from the venv as a stdio MCP server.

## Usage

Once configured, your AI agent can directly control Fusion 360:

> "Create a box with rounded edges, 50mm x 30mm x 20mm, with 2mm fillets"

> "Add a revolute joint between the arm and the base component, with ±90° limits"

> "Export the design as STEP"

All lengths are in **centimeters** (Fusion's internal unit). The tools accept cm and handle conversion automatically.

## Important Notes

- **Units**: All lengths in cm, all angles in degrees
- **Z-negation**: On XZ plane, sketch +Y maps to world -Z. On YZ plane, sketch +X maps to world -Z.
- **Thread safety**: The add-in uses Fusion's CustomEvent system to dispatch all API calls to the main thread
- **Joints**: Uses as-built joints by default (components stay in their current positions)
- **Escape hatch**: Use `execute_python` for any operation not covered by the curated tools

## Credits

Inspired by [ClaudeFusion360MCP](https://github.com/rahayesj/ClaudeFusion360MCP) (tool design, spatial awareness docs) and analysis of [Fusion-360-MCP-Server](https://github.com/AuraFriday/Fusion-360-MCP-Server) (thread-safe dispatch pattern).

## License

MIT
