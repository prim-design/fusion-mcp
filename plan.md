# Plan: Open-Source Fusion 360 MCP Server

## Goal
Build a fully open-source, two-component Fusion 360 MCP server with ~35 curated tools (including joints, rigid groups, all 7 joint types) plus an `execute_python` escape hatch, using proper thread-safe Fusion API dispatch. No closed-source binaries.

## Architecture

```
Claude Code ──(stdio)──▶ MCP Server (Python, FastMCP)
                              │
                         TCP localhost:52361
                              │
                              ▼
                    Fusion 360 Add-in (Python)
                         │
                    CustomEvent dispatch
                         │
                         ▼
                    Main thread → Fusion API
```

## Key Design Decisions

1. **stdio transport** for MCP (Claude Code native support, no middleman binary)
2. **TCP socket on localhost** for MCP↔Add-in communication (fast, reliable, no file polling)
3. **Newline-delimited JSON** protocol (simple, debuggable)
4. **CustomEvent + Queue** for thread safety (proven pattern from AuraFriday)
5. **Curated tools** for common ops + `execute_python` for everything else
6. **Object context store** (`store_as` / `$ref`) for multi-step operations
7. **As-built joints** by default (components stay in position, more intuitive)

## File Structure

```
fusion-mcp/
├── README.md
├── pyproject.toml                    # MCP server package (pip install)
├── server/
│   ├── __init__.py
│   └── fusion360_mcp_server.py       # FastMCP server with all tool definitions
├── addin/
│   ├── FusionMCP.py                  # Fusion 360 add-in entry point
│   ├── FusionMCP.manifest            # Fusion add-in manifest
│   ├── commands.py                   # Tool implementations (Fusion API calls)
│   └── thread_safe.py               # CustomEvent + Queue infrastructure
└── docs/
    └── SKILL.md                      # Claude skill file for spatial awareness
```

## Phased Todo List

### Phase 1: Infrastructure
- [ ] Create project structure and pyproject.toml
- [ ] Build TCP socket communication layer (server side — client in `fusion360_mcp_server.py`)
- [ ] Build TCP socket communication layer (add-in side — server in `FusionMCP.py`)
- [ ] Implement thread-safe CustomEvent + Queue dispatch in `thread_safe.py`
- [ ] Implement add-in entry point (`run`/`stop`) with socket server + event loop

### Phase 2: Core MCP Server Tools
- [ ] Sketch tools: `create_sketch`, `finish_sketch`, `draw_rectangle`, `draw_circle`, `draw_line`, `draw_arc`, `draw_polygon`, `draw_spline`, `draw_slot`
- [ ] 3D features: `extrude`, `revolve`, `sweep`, `loft`
- [ ] Modifications: `fillet`, `chamfer`, `shell`, `draft`
- [ ] Patterns: `pattern_rectangular`, `pattern_circular`, `mirror`
- [ ] Boolean: `combine` (cut/join/intersect), `split_body`

### Phase 3: Core Add-in Implementations
- [ ] Implement all sketch tool handlers
- [ ] Implement all 3D feature handlers
- [ ] Implement all modification handlers
- [ ] Implement pattern and boolean handlers

### Phase 4: Components, Joints & Assembly
- [ ] Component tools: `create_component`, `list_components`, `delete_component`, `move_component`, `rotate_component`
- [ ] Joint tools: `create_joint` (all 7 types via `joint_type` param), `create_as_built_joint`, `set_joint_limits`, `drive_joint`
- [ ] Rigid groups: `create_rigid_group`, `list_rigid_groups`, `delete_rigid_group`
- [ ] Implement all component handlers in add-in
- [ ] Implement all joint handlers in add-in (using As-Built joints by default)
- [ ] Implement rigid group handlers

### Phase 5: Inspection, Export & Escape Hatch
- [ ] Inspection tools: `get_design_info`, `get_body_info`, `list_joints`, `measure`, `check_interference`, `fit_view`
- [ ] Export/Import: `export_stl`, `export_step`, `export_3mf`, `import_mesh`
- [ ] Utility: `undo`, `delete_body`, `delete_sketch`, `batch`
- [ ] `execute_python` escape hatch (exec arbitrary Python in Fusion context)
- [ ] Implement all inspection, export, utility, and execute_python handlers

### Phase 6: Documentation & Polish
- [ ] Write SKILL.md with coordinate system rules, Z-negation, gotchas
- [ ] Write README.md with installation instructions
- [ ] Add Claude Code MCP config instructions

## Key Interfaces

### TCP Protocol (newline-delimited JSON)
```python
# Request (server → add-in)
{"id": "abc123", "method": "create_sketch", "params": {"plane": "XY", "offset": 0}}

# Response (add-in → server)
{"id": "abc123", "success": true, "result": {"sketch_name": "Sketch1"}}

# Error response
{"id": "abc123", "success": false, "error": "No active design"}
```

### Tool pattern (MCP server side)
```python
@mcp.tool()
def create_sketch(plane: str, offset: float = 0) -> dict:
    """Create a new sketch on XY, XZ, or YZ plane (offset in cm)."""
    return send_command("create_sketch", {"plane": plane, "offset": offset})
```

### Handler pattern (add-in side)
```python
def handle_create_sketch(params):
    plane_name = params.get('plane', 'XY')
    plane_map = {
        'XY': rootComp.xYConstructionPlane,
        'XZ': rootComp.xZConstructionPlane,
        'YZ': rootComp.yZConstructionPlane
    }
    plane = plane_map.get(plane_name)
    if not plane:
        return error(f"Invalid plane: {plane_name}")
    offset = params.get('offset', 0)
    if offset != 0:
        # Create offset construction plane
        planes = rootComp.constructionPlanes
        planeInput = planes.createInput()
        offsetVal = adsk.core.ValueInput.createByReal(offset)
        planeInput.setByOffset(plane, offsetVal)
        plane = planes.add(planeInput)
    sketch = rootComp.sketches.add(plane)
    return success({"sketch_name": sketch.name, "sketch_index": rootComp.sketches.count - 1})
```

### Joint tool interface
```python
@mcp.tool()
def create_joint(
    occurrence1: str,        # Component name or index
    occurrence2: str,        # Component name or index
    joint_type: str = "rigid",  # rigid|revolute|slider|cylindrical|pin_slot|planar|ball
    axis: str = "Z",         # Joint axis direction
    angle: float = 0,        # Initial angle (revolute, degrees)
    min_angle: float = None,  # Min rotation limit (degrees)
    max_angle: float = None,  # Max rotation limit (degrees)
    min_distance: float = None, # Min slide limit (cm)
    max_distance: float = None  # Max slide limit (cm)
) -> dict:
    """Create an as-built joint between two components."""
```

## Risks / Open Questions

1. **Fusion API version differences**: Some API methods may differ between Fusion versions. We target current (2025+).
2. **Sketch edit mode**: Some operations require being in sketch edit mode, others require exiting it. The add-in needs to track and manage this state.
3. **Component resolution**: Need robust way to find components by name or index, handling nested components.
4. **Profile selection**: When sketches have multiple profiles (e.g., rectangle with circle cutout), selecting the right one is tricky. We default to last profile but support `profile_index`.
