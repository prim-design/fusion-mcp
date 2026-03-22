# Research: Open-Source Fusion 360 MCP Server

## Reference Implementations Analyzed

### 1. rahayesj/ClaudeFusion360MCP
- **MCP Server**: FastMCP, stdio transport, ~35 tool declarations
- **Add-in**: File-based IPC via `~/fusion_mcp_comm/` (JSON command/response files)
- **Polling**: Server polls at 50ms, add-in polls at 100ms, 45s timeout
- **Critical finding**: Add-in only implements 9 of 30+ declared tools (create_sketch, draw_circle, draw_rectangle, extrude, revolve, fillet, finish_sketch, fit_view, get_design_info). The rest return "Unknown tool".
- **Thread safety**: None — runs Fusion API calls on a daemon thread directly (fragile, works only for simple ops)
- **Strengths**: Excellent documentation (SKILL.md, SPATIAL_AWARENESS.md with Z-negation rules, KNOWN_ISSUES.md), clean tool API design with good parameter defaults

### 2. AuraFriday/Fusion-360-MCP-Server
- **Add-in**: Connects to closed-source MCP-Link binary via Chrome Native Messaging + SSE
- **Architecture**: Generic API executor — no curated tools, just `api_path` resolution + `execute_python`
- **Thread safety**: Proper pattern using `CustomEvent` + `queue.Queue` + timer thread. Work items queued from daemon thread, processed on main thread via Fusion CustomEvent.
- **Key patterns**:
  - `_resolve_api_path()`: Navigates dotted paths with shortcuts (app, ui, design, rootComponent, $stored)
  - `_construct_object()`: Auto-constructs Point3D, Vector3D etc from JSON specs `{"type": "Point3D", "x": 0, "y": 0, "z": 0}`
  - `_resolve_argument()`: Resolves stored references, API paths, constructors, and literals
  - `_handle_python_execution()`: `exec(compile(code), globals())` with stdout capture and session persistence
  - `fusion_context`: Dict for storing objects between calls (`store_as` / `$variable`)
- **Strengths**: Full API access, proper thread safety, persistent context

## Key Fusion 360 API Constraints

1. **Main-thread only**: All `adsk.*` API calls MUST run on the main UI thread
2. **CustomEvent pattern**: The standard way to dispatch work from background threads to main thread
3. **Units**: Internal units are always **cm** (length) and **radians** (angles)
4. **Z-negation**: On XZ plane, sketch Y = -World Z. On YZ plane, sketch X = -World Z. (Documented gotcha)
5. **Face/edge indices are unstable**: After boolean ops or features, indices can change

## Joint API (from Autodesk docs + API reference)

- **Standard joints**: `rootComp.joints.createInput(geo1, geo2)` + motion type setter + `joints.add(input)`
- **As-built joints**: `rootComp.asBuiltJoints.createInput(occ1, occ2)` — components stay in place
- **7 types**: Rigid, Revolute, Slider, Cylindrical, PinSlot, Planar, Ball
- **JointGeometry**: Factory methods for faces, edges, points, cylinders
- **Joint limits**: Access via `joint.jointMotion` → cast to specific motion type → `.rotationLimits` / `.slideLimits`
- **Rigid groups**: `rootComp.rigidGroups.add(ObjectCollection, includeChildren)`

## Architecture Decision: TCP Socket vs File IPC

| Aspect | File-based (rahayesj) | TCP Socket |
|--------|----------------------|------------|
| Latency | ~100ms (polling) | ~1ms |
| Complexity | Simple | Moderate |
| Debugging | Easy (read JSON files) | Need logging |
| Reliability | Good (filesystem is reliable) | Good (localhost) |
| Cleanup | Need to handle stale files | Clean disconnect |

**Decision**: Use TCP socket on localhost for lower latency. Fall back to file-based if socket fails.

## Communication Protocol Design

```
MCP Server (stdio) ←→ TCP Socket (localhost:52361) ←→ Fusion Add-in
```

Message format (JSON over TCP with newline delimiter):
```json
{"id": "uuid", "method": "tool_name", "params": {...}}
→
{"id": "uuid", "success": true, "result": {...}}
```
