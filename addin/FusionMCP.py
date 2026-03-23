"""
FusionMCP Add-in
================
Fusion 360 add-in that listens for commands from the MCP server over TCP,
executes them on the main thread via CustomEvent dispatch, and returns results.

Install: Copy this folder to Fusion 360's AddIns directory, then enable via Shift+S.
"""

import adsk.core
import adsk.fusion
import traceback
import json
import math
import io
import contextlib
import socket
import threading
import queue

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

app: adsk.core.Application = None
ui: adsk.core.UserInterface = None

HOST = "127.0.0.1"
PORT = 52361

_stop_event = threading.Event()
_server_thread: threading.Thread = None
_custom_event: adsk.core.CustomEvent = None
_event_handler = None

CUSTOM_EVENT_ID = "FusionMCP_ProcessQueue"

# Work queue: items are (command_dict, result_queue)
_work_queue = queue.Queue()

# Persistent Python execution sessions
_python_sessions = {}


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------


def run(context):
    global app, ui, _server_thread, _custom_event, _event_handler
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Register custom event for main-thread dispatch
        _custom_event = app.registerCustomEvent(CUSTOM_EVENT_ID)
        _event_handler = _MainThreadHandler()
        _custom_event.add(_event_handler)

        # Start TCP server thread
        _stop_event.clear()
        _server_thread = threading.Thread(target=_tcp_server_loop, daemon=True)
        _server_thread.start()

        ui.messageBox(f"FusionMCP started — listening on {HOST}:{PORT}")
    except:
        if ui:
            ui.messageBox("FusionMCP failed to start:\n" + traceback.format_exc())


def stop(context):
    global _custom_event, _event_handler
    try:
        _stop_event.set()
        # Unblock the accept() call by connecting and immediately closing
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((HOST, PORT))
        except:
            pass
        if _custom_event:
            _custom_event.remove(_event_handler)
            app.unregisterCustomEvent(CUSTOM_EVENT_ID)
            _custom_event = None
            _event_handler = None
    except:
        pass


# ---------------------------------------------------------------------------
# TCP server (runs on daemon thread)
# ---------------------------------------------------------------------------


def _tcp_server_loop():
    """Accept TCP connections and dispatch commands."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(1.0)
    server.bind((HOST, PORT))
    server.listen(4)

    while not _stop_event.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        try:
            _handle_connection(conn)
        except Exception as e:
            try:
                error_resp = json.dumps({"success": False, "error": str(e)}) + "\n"
                conn.sendall(error_resp.encode("utf-8"))
            except:
                pass
        finally:
            conn.close()

    server.close()


def _handle_connection(conn: socket.socket):
    """Read one command, dispatch to main thread, send response."""
    conn.settimeout(60)
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk

    command = json.loads(data.decode("utf-8").strip())
    result_q = queue.Queue()
    _work_queue.put((command, result_q))

    # Fire custom event to wake up Fusion's main thread
    app.fireCustomEvent(CUSTOM_EVENT_ID)

    # Block until main thread processes the command
    response = result_q.get(timeout=55)
    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Main-thread event handler
# ---------------------------------------------------------------------------


class _MainThreadHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        """Called on Fusion's main thread when custom event fires."""
        # Process ALL queued work items
        while not _work_queue.empty():
            try:
                command, result_q = _work_queue.get_nowait()
            except queue.Empty:
                break

            try:
                result = _dispatch(command)
                result_q.put(result)
            except Exception as e:
                result_q.put({
                    "id": command.get("id"),
                    "success": False,
                    "error": f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}",
                })


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------


def _dispatch(command: dict) -> dict:
    """Route a command to the appropriate handler."""
    cmd_id = command.get("id")
    method = command.get("method", "")
    params = command.get("params", {})

    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design and method not in ("execute_python",):
            return _error(cmd_id, "No active Fusion 360 design. Open or create a design first.")

        # Batch: process multiple commands sequentially
        if method == "batch":
            return _handle_batch(cmd_id, params, design)

        handler = _HANDLERS.get(method)
        if handler:
            result = handler(params, design)
            return {"id": cmd_id, "success": True, "result": result}
        else:
            return _error(cmd_id, f"Unknown method: {method}")
    except Exception as e:
        return _error(cmd_id, f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}")


def _error(cmd_id, msg):
    return {"id": cmd_id, "success": False, "error": msg}


def _handle_batch(cmd_id, params, design):
    commands = params.get("commands", [])
    results = []
    for i, cmd in enumerate(commands):
        method = cmd.get("method", "")
        p = cmd.get("params", {})
        handler = _HANDLERS.get(method)
        if not handler:
            return _error(cmd_id, f"Batch command {i}: unknown method '{method}'")
        try:
            results.append({"method": method, "result": handler(p, design)})
        except Exception as e:
            return _error(cmd_id, f"Batch command {i} ({method}) failed: {e}")
    return {"id": cmd_id, "success": True, "result": {"batch_results": results, "count": len(results)}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_root(params, design):
    """Get the target component (root or named)."""
    comp_name = params.get("component")
    rootComp = design.rootComponent
    if not comp_name:
        return rootComp
    for occ in rootComp.allOccurrences:
        if occ.component.name == comp_name:
            return occ.component
    return rootComp


def _resolve_occurrence(ref, design):
    """Resolve an occurrence by name (str) or index (int/str-of-int)."""
    rootComp = design.rootComponent
    occs = rootComp.allOccurrences

    # Try as index
    try:
        idx = int(ref)
        if 0 <= idx < occs.count:
            return occs.item(idx)
    except (ValueError, TypeError):
        pass

    # Try as name
    if isinstance(ref, str):
        for i in range(occs.count):
            occ = occs.item(i)
            if occ.name == ref or occ.component.name == ref:
                return occ

    raise ValueError(f"Cannot find occurrence: {ref}")


def _get_body(params, design):
    """Get a body by index from the root component."""
    comp = _get_root(params, design)
    idx = params.get("body_index", -1)
    count = comp.bRepBodies.count
    if count == 0:
        raise ValueError("No bodies in component")
    if idx == -1:
        idx = count - 1
    if idx < 0 or idx >= count:
        raise ValueError(f"Body index {idx} out of range (0-{count - 1})")
    return comp.bRepBodies.item(idx)


def _get_sketch(params, design, index_key="sketch_index"):
    """Get a sketch by index or name."""
    comp = _get_root(params, design)
    idx = params.get(index_key, -1)
    count = comp.sketches.count
    if count == 0:
        raise ValueError("No sketches in component")
    if idx == -1:
        idx = count - 1
    if idx < 0 or idx >= count:
        raise ValueError(f"Sketch index {idx} out of range (0-{count - 1})")
    return comp.sketches.item(idx)


def _plane_map(rootComp):
    return {
        "XY": rootComp.xYConstructionPlane,
        "XZ": rootComp.xZConstructionPlane,
        "YZ": rootComp.yZConstructionPlane,
    }


def _axis_map(rootComp):
    return {
        "X": rootComp.xConstructionAxis,
        "Y": rootComp.yConstructionAxis,
        "Z": rootComp.zConstructionAxis,
    }


def _operation_map():
    return {
        "new": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }


# ---------------------------------------------------------------------------
# SKETCH HANDLERS
# ---------------------------------------------------------------------------


def _handle_create_sketch(params, design):
    comp = _get_root(params, design)
    plane_name = params.get("plane", "XY").upper()
    planes = _plane_map(comp)
    plane = planes.get(plane_name)
    if not plane:
        raise ValueError(f"Invalid plane: {plane_name}. Use XY, XZ, or YZ.")

    offset = params.get("offset", 0)
    if offset != 0:
        cplanes = comp.constructionPlanes
        cpInput = cplanes.createInput()
        cpInput.setByOffset(plane, adsk.core.ValueInput.createByReal(offset))
        plane = cplanes.add(cpInput)

    sketch = comp.sketches.add(plane)
    return {"sketch_name": sketch.name, "sketch_index": comp.sketches.count - 1}


def _handle_finish_sketch(params, design):
    # Setting activeEditObject to the root component exits sketch edit mode
    design.activeEditObject
    try:
        # This is the documented way to exit sketch mode
        root = design.rootComponent
        if design.activeEditObject and design.activeEditObject.classType == adsk.fusion.Sketch.classType():
            design.activeEditObject.isComputeDeferred = False
    except:
        pass
    return {"message": "Sketch finished"}


def _handle_draw_line(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    p1 = adsk.core.Point3D.create(params["x1"], params["y1"], 0)
    p2 = adsk.core.Point3D.create(params["x2"], params["y2"], 0)
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
    return {"line": line.entityToken}


def _handle_draw_rectangle(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    p1 = adsk.core.Point3D.create(params["x1"], params["y1"], 0)
    p2 = adsk.core.Point3D.create(params["x2"], params["y2"], 0)
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
    return {"profiles": sketch.profiles.count}


def _handle_draw_circle(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    center = adsk.core.Point3D.create(params["center_x"], params["center_y"], 0)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(center, params["radius"])
    return {"profiles": sketch.profiles.count}


def _handle_draw_arc(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    center = adsk.core.Point3D.create(params["center_x"], params["center_y"], 0)
    start = adsk.core.Point3D.create(params["start_x"], params["start_y"], 0)
    end = adsk.core.Point3D.create(params["end_x"], params["end_y"], 0)
    # Compute sweep angle for the arc
    sketch.sketchCurves.sketchArcs.addByCenterStartEnd(center, start, end)
    return {"profiles": sketch.profiles.count}


def _handle_draw_polygon(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    cx, cy = params["center_x"], params["center_y"]
    radius = params["radius"]
    sides = params.get("sides", 6)
    lines = sketch.sketchCurves.sketchLines
    points = []
    for i in range(sides):
        angle = 2 * math.pi * i / sides - math.pi / 2  # start from top
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append(adsk.core.Point3D.create(x, y, 0))
    for i in range(sides):
        lines.addByTwoPoints(points[i], points[(i + 1) % sides])
    return {"sides": sides, "profiles": sketch.profiles.count}


def _handle_draw_spline(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    pts_data = params["points"]
    points = adsk.core.ObjectCollection.create()
    for p in pts_data:
        points.add(adsk.core.Point3D.create(p[0], p[1], 0))
    sketch.sketchCurves.sketchFittedSplines.add(points)
    return {"point_count": len(pts_data)}


def _handle_draw_slot(params, design):
    sketch = adsk.fusion.Sketch.cast(design.activeEditObject)
    if not sketch:
        raise ValueError("No active sketch. Call create_sketch first.")
    c1 = adsk.core.Point3D.create(params["center1_x"], params["center1_y"], 0)
    c2 = adsk.core.Point3D.create(params["center2_x"], params["center2_y"], 0)
    width = params["width"]
    # Draw slot as two arcs and two lines
    dx = c2.x - c1.x
    dy = c2.y - c1.y
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-8:
        raise ValueError("Slot centers must be different points")
    nx = -dy / length * (width / 2)
    ny = dx / length * (width / 2)
    p1 = adsk.core.Point3D.create(c1.x + nx, c1.y + ny, 0)
    p2 = adsk.core.Point3D.create(c2.x + nx, c2.y + ny, 0)
    p3 = adsk.core.Point3D.create(c2.x - nx, c2.y - ny, 0)
    p4 = adsk.core.Point3D.create(c1.x - nx, c1.y - ny, 0)
    lines = sketch.sketchCurves.sketchLines
    arcs = sketch.sketchCurves.sketchArcs
    lines.addByTwoPoints(p1, p2)
    arcs.addByCenterStartEnd(c2, p2, p3)
    lines.addByTwoPoints(p3, p4)
    arcs.addByCenterStartEnd(c1, p4, p1)
    return {"profiles": sketch.profiles.count}


# ---------------------------------------------------------------------------
# 3D FEATURE HANDLERS
# ---------------------------------------------------------------------------


def _handle_extrude(params, design):
    comp = _get_root(params, design)
    profile_index = params.get("profile_index", -1)
    sketch = comp.sketches.item(comp.sketches.count - 1)
    if sketch.profiles.count == 0:
        raise ValueError("No profiles in the last sketch")
    if profile_index == -1:
        profile_index = sketch.profiles.count - 1
    profile = sketch.profiles.item(profile_index)

    op = _operation_map().get(params.get("operation", "new"))
    extrudes = comp.features.extrudeFeatures
    extInput = extrudes.createInput(profile, op)

    distance = params["distance"]
    taper = params.get("taper_angle", 0)

    extent = adsk.fusion.DistanceExtentDefinition.create(
        adsk.core.ValueInput.createByReal(abs(distance))
    )
    if taper != 0:
        extent.taperAngle = adsk.core.ValueInput.createByReal(math.radians(taper))

    if distance >= 0:
        extInput.setOneSideExtent(extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
    else:
        extInput.setOneSideExtent(extent, adsk.fusion.ExtentDirections.NegativeExtentDirection)

    feature = extrudes.add(extInput)
    return {"feature_name": feature.name, "bodies": feature.bodies.count}


def _handle_revolve(params, design):
    comp = _get_root(params, design)
    profile_index = params.get("profile_index", -1)
    sketch = comp.sketches.item(comp.sketches.count - 1)
    if sketch.profiles.count == 0:
        raise ValueError("No profiles in the last sketch")
    if profile_index == -1:
        profile_index = sketch.profiles.count - 1
    profile = sketch.profiles.item(profile_index)

    axis_name = params.get("axis", "Y").upper()
    axis = _axis_map(comp).get(axis_name)
    if not axis:
        raise ValueError(f"Invalid axis: {axis_name}")

    revolves = comp.features.revolveFeatures
    revInput = revolves.createInput(profile, axis, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    angle_deg = params.get("angle", 360)
    revInput.setAngleExtent(False, adsk.core.ValueInput.createByReal(math.radians(angle_deg)))
    feature = revolves.add(revInput)
    return {"feature_name": feature.name}


def _handle_sweep(params, design):
    comp = _get_root(params, design)
    # Find profile sketch
    prof_sketch = None
    path_sketch = None
    for i in range(comp.sketches.count):
        s = comp.sketches.item(i)
        if s.name == params["profile_sketch"]:
            prof_sketch = s
        if s.name == params["path_sketch"]:
            path_sketch = s
    if not prof_sketch:
        raise ValueError(f"Profile sketch '{params['profile_sketch']}' not found")
    if not path_sketch:
        raise ValueError(f"Path sketch '{params['path_sketch']}' not found")
    if prof_sketch.profiles.count == 0:
        raise ValueError("Profile sketch has no profiles")

    profile = prof_sketch.profiles.item(0)

    # Create path from first curve in path sketch
    path = comp.features.createPath(path_sketch.sketchCurves.item(0))

    sweeps = comp.features.sweepFeatures
    sweepInput = sweeps.createInput(profile, path, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    feature = sweeps.add(sweepInput)
    return {"feature_name": feature.name}


def _handle_loft(params, design):
    comp = _get_root(params, design)
    sketch_names = params["profile_sketches"]
    loftSections = adsk.core.ObjectCollection.create()
    for name in sketch_names:
        found = False
        for i in range(comp.sketches.count):
            s = comp.sketches.item(i)
            if s.name == name:
                if s.profiles.count == 0:
                    raise ValueError(f"Sketch '{name}' has no profiles")
                loftSections.add(s.profiles.item(0))
                found = True
                break
        if not found:
            raise ValueError(f"Sketch '{name}' not found")

    lofts = comp.features.loftFeatures
    loftInput = lofts.createInput(adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    for i in range(loftSections.count):
        loftInput.loftSections.add(loftSections.item(i))
    feature = lofts.add(loftInput)
    return {"feature_name": feature.name}


# ---------------------------------------------------------------------------
# MODIFICATION HANDLERS
# ---------------------------------------------------------------------------


def _handle_fillet(params, design):
    body = _get_body(params, design)
    radius = params["radius"]
    edge_indices = params.get("edges")

    edges = adsk.core.ObjectCollection.create()
    if edge_indices is not None:
        for idx in edge_indices:
            if 0 <= idx < body.edges.count:
                edges.add(body.edges.item(idx))
    else:
        for edge in body.edges:
            edges.add(edge)

    comp = body.parentComponent
    fillets = comp.features.filletFeatures
    fInput = fillets.createInput()
    fInput.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal(radius), True)
    feature = fillets.add(fInput)
    return {"feature_name": feature.name}


def _handle_chamfer(params, design):
    body = _get_body(params, design)
    distance = params["distance"]
    edge_indices = params.get("edges")

    edges = adsk.core.ObjectCollection.create()
    if edge_indices is not None:
        for idx in edge_indices:
            if 0 <= idx < body.edges.count:
                edges.add(body.edges.item(idx))
    else:
        for edge in body.edges:
            edges.add(edge)

    comp = body.parentComponent
    chamfers = comp.features.chamferFeatures
    cInput = chamfers.createInput2()
    cInput.chamferEdgeSets.addEqualDistanceChamferEdgeSet(
        edges, adsk.core.ValueInput.createByReal(distance), True
    )
    feature = chamfers.add(cInput)
    return {"feature_name": feature.name}


def _handle_shell(params, design):
    body = _get_body(params, design)
    thickness = params["thickness"]
    face_indices = params.get("faces_to_remove")

    comp = body.parentComponent
    shells = comp.features.shellFeatures

    faces = adsk.core.ObjectCollection.create()
    if face_indices:
        for idx in face_indices:
            if 0 <= idx < body.faces.count:
                faces.add(body.faces.item(idx))

    sInput = shells.createInput([body] if not face_indices else None)
    if face_indices and faces.count > 0:
        sInput = shells.createInput(faces)

    sInput.insideThickness = adsk.core.ValueInput.createByReal(thickness)
    feature = shells.add(sInput)
    return {"feature_name": feature.name}


def _handle_draft(params, design):
    body = _get_body(params, design)
    angle_deg = params["angle"]
    face_indices = params.get("faces")
    pull_dir = params.get("pull_direction", [0, 0, 1])

    comp = body.parentComponent

    faces = adsk.core.ObjectCollection.create()
    if face_indices is not None:
        for idx in face_indices:
            if 0 <= idx < body.faces.count:
                faces.add(body.faces.item(idx))
    else:
        for face in body.faces:
            faces.add(face)

    # Use the first planar face as the draft plane, or construct one
    drafts = comp.features.draftFeatures
    # Find a reference plane
    plane = comp.xYConstructionPlane
    dInput = drafts.createInput(
        faces, plane,
        adsk.core.ValueInput.createByReal(math.radians(angle_deg)),
        False  # is symmetric
    )
    feature = drafts.add(dInput)
    return {"feature_name": feature.name}


# ---------------------------------------------------------------------------
# PATTERN HANDLERS
# ---------------------------------------------------------------------------


def _handle_pattern_rectangular(params, design):
    body = _get_body(params, design)
    comp = body.parentComponent

    inputEntities = adsk.core.ObjectCollection.create()
    inputEntities.add(body)

    patterns = comp.features.rectangularPatternFeatures
    pInput = patterns.createInput(inputEntities, comp.xConstructionAxis)
    pInput.quantityOne = adsk.core.ValueInput.createByReal(params["x_count"])
    pInput.distanceOne = adsk.core.ValueInput.createByReal(params["x_spacing"])

    y_count = params.get("y_count", 1)
    if y_count > 1:
        pInput.setDirectionTwo(comp.yConstructionAxis)
        pInput.quantityTwo = adsk.core.ValueInput.createByReal(y_count)
        pInput.distanceTwo = adsk.core.ValueInput.createByReal(params.get("y_spacing", 0))

    feature = patterns.add(pInput)
    return {"feature_name": feature.name}


def _handle_pattern_circular(params, design):
    body = _get_body(params, design)
    comp = body.parentComponent

    inputEntities = adsk.core.ObjectCollection.create()
    inputEntities.add(body)

    axis_name = params.get("axis", "Z").upper()
    axis = _axis_map(comp).get(axis_name)

    patterns = comp.features.circularPatternFeatures
    pInput = patterns.createInput(inputEntities, axis)
    pInput.quantity = adsk.core.ValueInput.createByReal(params["count"])
    pInput.totalAngle = adsk.core.ValueInput.createByReal(math.radians(params.get("angle", 360)))
    feature = patterns.add(pInput)
    return {"feature_name": feature.name}


def _handle_mirror(params, design):
    body = _get_body(params, design)
    comp = body.parentComponent

    inputEntities = adsk.core.ObjectCollection.create()
    inputEntities.add(body)

    plane_name = params.get("plane", "YZ").upper()
    plane = _plane_map(comp).get(plane_name)

    mirrors = comp.features.mirrorFeatures
    mInput = mirrors.createInput(inputEntities, plane)
    feature = mirrors.add(mInput)
    return {"feature_name": feature.name}


# ---------------------------------------------------------------------------
# BOOLEAN HANDLERS
# ---------------------------------------------------------------------------


def _handle_combine(params, design):
    rootComp = design.rootComponent
    target_idx = params["target_body"]
    tool_indices = params["tool_bodies"]
    op_name = params.get("operation", "cut")
    keep = params.get("keep_tools", False)

    target = rootComp.bRepBodies.item(target_idx)
    tools = adsk.core.ObjectCollection.create()
    for idx in tool_indices:
        tools.add(rootComp.bRepBodies.item(idx))

    op_map = {
        "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
        "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }
    op = op_map.get(op_name)
    if not op:
        raise ValueError(f"Invalid operation: {op_name}")

    combines = rootComp.features.combineFeatures
    cInput = combines.createInput(target, tools)
    cInput.operation = op
    cInput.isKeepToolBodies = keep
    feature = combines.add(cInput)
    return {"feature_name": feature.name}


def _handle_split_body(params, design):
    comp = _get_root(params, design)
    body = comp.bRepBodies.item(params["body_index"])
    split_ref = params.get("split_tool", "XY").upper()

    plane = _plane_map(comp).get(split_ref)
    if not plane:
        raise ValueError(f"Invalid split tool: {split_ref}")

    splits = comp.features.splitBodyFeatures
    sInput = splits.createInput(body, plane, True)
    feature = splits.add(sInput)
    return {"feature_name": feature.name}


# ---------------------------------------------------------------------------
# COMPONENT HANDLERS
# ---------------------------------------------------------------------------


def _handle_create_component(params, design):
    rootComp = design.rootComponent
    body_idx = params.get("from_body", -1)
    if body_idx == -1:
        body_idx = rootComp.bRepBodies.count - 1
    body = rootComp.bRepBodies.item(body_idx)

    # Create new occurrence with empty component
    occ = rootComp.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    name = params.get("name")
    if name:
        comp.name = name

    # Move body to new component
    body.moveToComponent(occ)

    return {"component_name": comp.name, "occurrence_name": occ.name}


def _handle_list_components(params, design):
    rootComp = design.rootComponent
    components = []
    for i in range(rootComp.allOccurrences.count):
        occ = rootComp.allOccurrences.item(i)
        transform = occ.transform
        pos = transform.translation
        components.append({
            "index": i,
            "name": occ.name,
            "component_name": occ.component.name,
            "position": {"x": round(pos.x, 4), "y": round(pos.y, 4), "z": round(pos.z, 4)},
            "bodies": occ.bRepBodies.count,
        })
    return {"components": components, "count": len(components)}


def _handle_delete_component(params, design):
    rootComp = design.rootComponent
    name = params.get("name")
    index = params.get("index")
    if index is not None:
        occ = rootComp.allOccurrences.item(index)
    elif name:
        occ = _resolve_occurrence(name, design)
    else:
        raise ValueError("Provide name or index")
    occ.deleteMe()
    return {"deleted": True}


def _handle_rename(params, design):
    rootComp = design.rootComponent
    target_type = params.get("target_type", "component").lower()
    new_name = params["new_name"]
    target = params.get("target")
    index = params.get("index")

    if target_type == "component":
        if index is not None:
            occ = rootComp.allOccurrences.item(int(index))
        else:
            occ = _resolve_occurrence(target, design)
        old_name = occ.component.name
        occ.component.name = new_name
        return {"old_name": old_name, "new_name": new_name, "type": "component"}

    elif target_type == "body":
        idx = int(index) if index is not None else int(target) if target.isdigit() else -1
        if idx == -1:
            # Search by name
            for i in range(rootComp.bRepBodies.count):
                if rootComp.bRepBodies.item(i).name == target:
                    idx = i
                    break
        if idx == -1:
            raise ValueError(f"Body not found: {target}")
        body = rootComp.bRepBodies.item(idx)
        old_name = body.name
        body.name = new_name
        return {"old_name": old_name, "new_name": new_name, "type": "body"}

    elif target_type == "sketch":
        idx = int(index) if index is not None else int(target) if target.isdigit() else -1
        if idx == -1:
            for i in range(rootComp.sketches.count):
                if rootComp.sketches.item(i).name == target:
                    idx = i
                    break
        if idx == -1:
            raise ValueError(f"Sketch not found: {target}")
        sketch = rootComp.sketches.item(idx)
        old_name = sketch.name
        sketch.name = new_name
        return {"old_name": old_name, "new_name": new_name, "type": "sketch"}

    elif target_type == "joint":
        joint = None
        # Search as-built joints
        for i in range(rootComp.asBuiltJoints.count):
            j = rootComp.asBuiltJoints.item(i)
            if j.name == target or (index is not None and i == int(index)):
                joint = j
                break
        # Search standard joints
        if not joint:
            for i in range(rootComp.joints.count):
                j = rootComp.joints.item(i)
                if j.name == target or (index is not None and i == int(index)):
                    joint = j
                    break
        if not joint:
            raise ValueError(f"Joint not found: {target}")
        old_name = joint.name
        joint.name = new_name
        return {"old_name": old_name, "new_name": new_name, "type": "joint"}

    else:
        raise ValueError(f"Invalid target_type: {target_type}. Use component, body, sketch, or joint.")


def _handle_move_component(params, design):
    occ = None
    if params.get("name"):
        occ = _resolve_occurrence(params["name"], design)
    elif params.get("index") is not None:
        occ = _resolve_occurrence(params["index"], design)
    else:
        raise ValueError("Provide name or index")

    x, y, z = params.get("x", 0), params.get("y", 0), params.get("z", 0)
    absolute = params.get("absolute", True)

    transform = occ.transform
    if absolute:
        transform.translation = adsk.core.Vector3D.create(x, y, z)
    else:
        current = transform.translation
        transform.translation = adsk.core.Vector3D.create(
            current.x + x, current.y + y, current.z + z
        )
    occ.transform = transform
    design.snapshots.add()
    pos = occ.transform.translation
    return {"position": {"x": round(pos.x, 4), "y": round(pos.y, 4), "z": round(pos.z, 4)}}


def _handle_rotate_component(params, design):
    occ = None
    if params.get("name"):
        occ = _resolve_occurrence(params["name"], design)
    elif params.get("index") is not None:
        occ = _resolve_occurrence(params["index"], design)
    else:
        raise ValueError("Provide name or index")

    angle_deg = params["angle"]
    axis_str = params.get("axis", "Z").upper()
    origin = adsk.core.Point3D.create(
        params.get("origin_x", 0), params.get("origin_y", 0), params.get("origin_z", 0)
    )
    axis_vectors = {
        "X": adsk.core.Vector3D.create(1, 0, 0),
        "Y": adsk.core.Vector3D.create(0, 1, 0),
        "Z": adsk.core.Vector3D.create(0, 0, 1),
    }
    axis_vec = axis_vectors.get(axis_str)
    if not axis_vec:
        raise ValueError(f"Invalid axis: {axis_str}")

    transform = occ.transform
    transform.setToRotation(math.radians(angle_deg), axis_vec, origin)
    occ.transform = transform
    design.snapshots.add()
    return {"rotated": True}


# ---------------------------------------------------------------------------
# JOINT HANDLERS
# ---------------------------------------------------------------------------


def _handle_create_joint(params, design):
    rootComp = design.rootComponent
    occ1 = _resolve_occurrence(params["occurrence1"], design)
    occ2 = _resolve_occurrence(params["occurrence2"], design)

    joint_type_str = params.get("joint_type", "rigid").lower()
    axis_str = params.get("axis", "Z").upper()

    # Create as-built joint (components stay in place)
    # geometry is required: None for rigid joints, best cylindrical face for others
    asBuiltJoints = rootComp.asBuiltJoints
    if joint_type_str == "rigid":
        geo = None
    else:
        # Try to find a cylindrical face on child component for proper joint placement
        geo = None
        if occ2.bRepBodies.count > 0:
            body = occ2.bRepBodies.item(0)
            for fi in range(body.faces.count):
                face = body.faces.item(fi)
                if face.geometry and face.geometry.surfaceType == 1:  # Cylinder
                    try:
                        geo = adsk.fusion.JointGeometry.createByCylinderOrConeFace(
                            face,
                            adsk.fusion.JointQuadrantAngleTypes.StartJointQuadrantAngleType,
                            adsk.fusion.JointKeyPointTypes.MiddleKeyPoint,
                        )
                        break
                    except:
                        continue
        # Fallback to origin construction point
        if geo is None:
            geo = adsk.fusion.JointGeometry.createByPoint(rootComp.originConstructionPoint)
    abInput = asBuiltJoints.createInput(occ1, occ2, geo)

    JD = adsk.fusion.JointDirections
    axis_map = {
        "X": JD.XAxisJointDirection,
        "Y": JD.YAxisJointDirection,
        "Z": JD.ZAxisJointDirection,
    }
    direction = axis_map.get(axis_str, JD.ZAxisJointDirection)

    if joint_type_str == "rigid":
        abInput.setAsRigidJointMotion()
    elif joint_type_str == "revolute":
        abInput.setAsRevoluteJointMotion(direction)
    elif joint_type_str == "slider":
        abInput.setAsSliderJointMotion(direction)
    elif joint_type_str == "cylindrical":
        abInput.setAsCylindricalJointMotion(direction)
    elif joint_type_str == "pin_slot":
        # pin_slot needs two axes: rotation and slide
        slide_axis_str = params.get("slide_axis", "X").upper()
        slide_dir = axis_map.get(slide_axis_str, JD.XAxisJointDirection)
        abInput.setAsPinSlotJointMotion(direction, slide_dir)
    elif joint_type_str == "planar":
        abInput.setAsPlanarJointMotion(direction)
    elif joint_type_str == "ball":
        abInput.setAsBallJointMotion()
    else:
        raise ValueError(
            f"Invalid joint_type: {joint_type_str}. "
            "Use: rigid, revolute, slider, cylindrical, pin_slot, planar, ball"
        )

    joint = asBuiltJoints.add(abInput)

    # Set name if provided
    joint_name = params.get("name")
    if joint_name:
        joint.name = joint_name

    # Set initial angle/offset if provided
    angle = params.get("angle", 0)
    offset = params.get("offset", 0)

    if angle != 0:
        motion = joint.jointMotion
        if hasattr(motion, "rotationValue"):
            motion.rotationValue = math.radians(angle)

    if offset != 0:
        motion = joint.jointMotion
        if hasattr(motion, "slideValue"):
            motion.slideValue = offset

    # Set limits inline if provided
    motion = joint.jointMotion
    if hasattr(motion, "rotationLimits"):
        limits = motion.rotationLimits
        if params.get("min_angle") is not None:
            limits.isMinimumValueEnabled = True
            limits.minimumValue = math.radians(params["min_angle"])
        if params.get("max_angle") is not None:
            limits.isMaximumValueEnabled = True
            limits.maximumValue = math.radians(params["max_angle"])

    if hasattr(motion, "slideLimits"):
        limits = motion.slideLimits
        if params.get("min_distance") is not None:
            limits.isMinimumValueEnabled = True
            limits.minimumValue = params["min_distance"]
        if params.get("max_distance") is not None:
            limits.isMaximumValueEnabled = True
            limits.maximumValue = params["max_distance"]

    return {"joint_name": joint.name, "joint_type": joint_type_str}


def _handle_set_joint_limits(params, design):
    rootComp = design.rootComponent
    joint_ref = params["joint"]

    # Find joint
    joint = None
    # Try as-built joints
    for i in range(rootComp.asBuiltJoints.count):
        j = rootComp.asBuiltJoints.item(i)
        if j.name == joint_ref:
            joint = j
            break
    # Try by index
    if joint is None:
        try:
            idx = int(joint_ref)
            if 0 <= idx < rootComp.asBuiltJoints.count:
                joint = rootComp.asBuiltJoints.item(idx)
        except (ValueError, TypeError):
            pass
    # Try standard joints
    if joint is None:
        for i in range(rootComp.joints.count):
            j = rootComp.joints.item(i)
            if j.name == joint_ref:
                joint = j
                break

    if not joint:
        raise ValueError(f"Joint not found: {joint_ref}")

    motion = joint.jointMotion

    # Rotation limits
    if hasattr(motion, "rotationLimits"):
        limits = motion.rotationLimits
        if params.get("min_angle") is not None:
            limits.isMinimumValueEnabled = True
            limits.minimumValue = math.radians(params["min_angle"])
        if params.get("max_angle") is not None:
            limits.isMaximumValueEnabled = True
            limits.maximumValue = math.radians(params["max_angle"])
        if params.get("rest_angle") is not None:
            limits.restValue = math.radians(params["rest_angle"])

    # Slide limits
    if hasattr(motion, "slideLimits"):
        limits = motion.slideLimits
        if params.get("min_distance") is not None:
            limits.isMinimumValueEnabled = True
            limits.minimumValue = params["min_distance"]
        if params.get("max_distance") is not None:
            limits.isMaximumValueEnabled = True
            limits.maximumValue = params["max_distance"]
        if params.get("rest_distance") is not None:
            limits.restValue = params["rest_distance"]

    return {"joint_name": joint.name, "limits_set": True}


def _handle_drive_joint(params, design):
    rootComp = design.rootComponent
    joint_ref = params["joint"]

    # Find joint (same logic as set_joint_limits)
    joint = None
    for i in range(rootComp.asBuiltJoints.count):
        j = rootComp.asBuiltJoints.item(i)
        if j.name == joint_ref:
            joint = j
            break
    if joint is None:
        try:
            idx = int(joint_ref)
            if 0 <= idx < rootComp.asBuiltJoints.count:
                joint = rootComp.asBuiltJoints.item(idx)
        except (ValueError, TypeError):
            pass
    if joint is None:
        for i in range(rootComp.joints.count):
            j = rootComp.joints.item(i)
            if j.name == joint_ref:
                joint = j
                break
    if not joint:
        raise ValueError(f"Joint not found: {joint_ref}")

    motion = joint.jointMotion
    if params.get("angle") is not None and hasattr(motion, "rotationValue"):
        motion.rotationValue = math.radians(params["angle"])
    if params.get("distance") is not None and hasattr(motion, "slideValue"):
        motion.slideValue = params["distance"]

    return {"joint_name": joint.name, "driven": True}


def _handle_list_joints(params, design):
    rootComp = design.rootComponent
    joints_list = []

    for i in range(rootComp.asBuiltJoints.count):
        j = rootComp.asBuiltJoints.item(i)
        info = {
            "index": i,
            "name": j.name,
            "type": "as_built",
            "joint_motion_type": str(j.jointMotion.jointType) if j.jointMotion else "unknown",
        }
        try:
            info["occurrence1"] = j.occurrenceOne.name
            info["occurrence2"] = j.occurrenceTwo.name
        except:
            pass
        joints_list.append(info)

    for i in range(rootComp.joints.count):
        j = rootComp.joints.item(i)
        info = {
            "index": i,
            "name": j.name,
            "type": "standard",
            "joint_motion_type": str(j.jointMotion.jointType) if j.jointMotion else "unknown",
        }
        try:
            info["occurrence1"] = j.occurrenceOne.name
            info["occurrence2"] = j.occurrenceTwo.name
        except:
            pass
        joints_list.append(info)

    return {"joints": joints_list, "count": len(joints_list)}


# ---------------------------------------------------------------------------
# RIGID GROUP HANDLERS
# ---------------------------------------------------------------------------


def _handle_create_rigid_group(params, design):
    rootComp = design.rootComponent
    occ_refs = params["occurrences"]

    occs = adsk.core.ObjectCollection.create()
    for ref in occ_refs:
        occs.add(_resolve_occurrence(ref, design))

    rg = rootComp.rigidGroups.add(occs, True)
    name = params.get("name")
    if name:
        rg.name = name

    return {"rigid_group_name": rg.name}


def _handle_list_rigid_groups(params, design):
    rootComp = design.rootComponent
    groups = []
    for i in range(rootComp.rigidGroups.count):
        rg = rootComp.rigidGroups.item(i)
        members = []
        for j in range(rg.occurrences.count):
            members.append(rg.occurrences.item(j).name)
        groups.append({"index": i, "name": rg.name, "members": members})
    return {"rigid_groups": groups, "count": len(groups)}


def _handle_delete_rigid_group(params, design):
    rootComp = design.rootComponent
    name = params.get("name")
    index = params.get("index")
    if index is not None:
        rg = rootComp.rigidGroups.item(index)
    elif name:
        rg = None
        for i in range(rootComp.rigidGroups.count):
            if rootComp.rigidGroups.item(i).name == name:
                rg = rootComp.rigidGroups.item(i)
                break
        if not rg:
            raise ValueError(f"Rigid group not found: {name}")
    else:
        raise ValueError("Provide name or index")
    rg.deleteMe()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# INSPECTION HANDLERS
# ---------------------------------------------------------------------------


def _handle_get_design_info(params, design):
    rootComp = design.rootComponent
    return {
        "design_name": design.parentDocument.name if design.parentDocument else "Untitled",
        "body_count": rootComp.bRepBodies.count,
        "sketch_count": rootComp.sketches.count,
        "component_count": rootComp.allOccurrences.count,
        "joint_count": rootComp.joints.count + rootComp.asBuiltJoints.count,
        "rigid_group_count": rootComp.rigidGroups.count,
        "active_sketch": design.activeEditObject.name
        if design.activeEditObject
        and design.activeEditObject.classType == adsk.fusion.Sketch.classType()
        else None,
    }


def _handle_get_body_info(params, design):
    body = _get_body(params, design)
    edges = []
    for i in range(body.edges.count):
        edge = body.edges.item(i)
        edges.append({
            "index": i,
            "length": round(edge.length, 4),
            "type": str(edge.geometry.curveType) if edge.geometry else "unknown",
        })
    faces = []
    for i in range(body.faces.count):
        face = body.faces.item(i)
        faces.append({
            "index": i,
            "area": round(face.area, 4),
            "type": str(face.geometry.surfaceType) if face.geometry else "unknown",
        })
    return {
        "body_name": body.name,
        "edges": edges,
        "edge_count": len(edges),
        "faces": faces,
        "face_count": len(faces),
    }


def _handle_measure(params, design):
    target = params.get("target", "body")
    body = _get_body(params, design)

    if target == "body":
        bbox = body.boundingBox
        return {
            "volume": round(body.volume, 6),
            "surface_area": round(body.area, 4),
            "bounding_box": {
                "min": {"x": round(bbox.minPoint.x, 4), "y": round(bbox.minPoint.y, 4), "z": round(bbox.minPoint.z, 4)},
                "max": {"x": round(bbox.maxPoint.x, 4), "y": round(bbox.maxPoint.y, 4), "z": round(bbox.maxPoint.z, 4)},
                "size": {
                    "x": round(bbox.maxPoint.x - bbox.minPoint.x, 4),
                    "y": round(bbox.maxPoint.y - bbox.minPoint.y, 4),
                    "z": round(bbox.maxPoint.z - bbox.minPoint.z, 4),
                },
            },
        }
    elif target == "edge":
        idx = params.get("edge_index", 0)
        edge = body.edges.item(idx)
        return {"edge_index": idx, "length": round(edge.length, 4)}
    elif target == "face":
        idx = params.get("face_index", 0)
        face = body.faces.item(idx)
        return {"face_index": idx, "area": round(face.area, 4)}
    else:
        raise ValueError(f"Invalid target: {target}")


def _handle_check_interference(params, design):
    rootComp = design.rootComponent
    occs = rootComp.allOccurrences
    collisions = []
    for i in range(occs.count):
        for j in range(i + 1, occs.count):
            bb1 = occs.item(i).boundingBox
            bb2 = occs.item(j).boundingBox
            if bb1.intersects(bb2):
                collisions.append({
                    "component1": occs.item(i).name,
                    "component2": occs.item(j).name,
                })
    return {"collisions": collisions, "has_interference": len(collisions) > 0}


def _handle_fit_view(params, design):
    app.activeViewport.fit()
    return {"fitted": True}


def _handle_screenshot(params, design):
    import tempfile
    import base64
    import os

    width = params.get("width", 1920)
    height = params.get("height", 1080)
    view = params.get("view")
    fit = params.get("fit", True)

    viewport = app.activeViewport
    camera = viewport.camera

    # Preset views: eye positions for common angles (looking at origin)
    d = 50  # default distance from origin
    VIEW_PRESETS = {
        "front":    (0,  0,  d),
        "back":     (0,  0, -d),
        "top":      (0,  d,  0),
        "bottom":   (0, -d,  0),
        "left":     (-d, 0,  0),
        "right":    (d,  0,  0),
        "iso":      (d,  d,  d),
        "iso_back": (-d, d, -d),
    }

    # Apply custom eye position or view preset
    eye_x = params.get("eye_x")
    if eye_x is not None:
        # Custom camera position
        eye = adsk.core.Point3D.create(
            eye_x,
            params.get("eye_y", 0),
            params.get("eye_z", 0),
        )
        target = adsk.core.Point3D.create(
            params.get("target_x", 0),
            params.get("target_y", 0),
            params.get("target_z", 0),
        )
        camera.eye = eye
        camera.target = target
        camera.upVector = adsk.core.Vector3D.create(0, 1, 0)
        camera.isSmoothTransition = False
        viewport.camera = camera
    elif view and view.lower() in VIEW_PRESETS:
        ex, ey, ez = VIEW_PRESETS[view.lower()]
        camera.eye = adsk.core.Point3D.create(ex, ey, ez)
        camera.target = adsk.core.Point3D.create(0, 0, 0)
        camera.upVector = adsk.core.Vector3D.create(0, 1, 0)
        camera.isSmoothTransition = False
        viewport.camera = camera

    if fit:
        viewport.fit()

    # Save viewport to a temp PNG file
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        viewport.saveAsImageFile(tmp_path, width, height)
        with open(tmp_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        return {"image_base64": image_b64, "width": width, "height": height}
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


# ---------------------------------------------------------------------------
# EXPORT / IMPORT HANDLERS
# ---------------------------------------------------------------------------


def _handle_export_stl(params, design):
    filepath = params["filepath"]
    rootComp = design.rootComponent
    exportMgr = design.exportManager
    stlOptions = exportMgr.createSTLExportOptions(rootComp, filepath)
    exportMgr.execute(stlOptions)
    return {"exported": filepath}


def _handle_export_step(params, design):
    filepath = params["filepath"]
    rootComp = design.rootComponent
    exportMgr = design.exportManager
    stepOptions = exportMgr.createSTEPExportOptions(filepath, rootComp)
    exportMgr.execute(stepOptions)
    return {"exported": filepath}


def _handle_export_3mf(params, design):
    filepath = params["filepath"]
    rootComp = design.rootComponent
    exportMgr = design.exportManager
    options = exportMgr.createC3MFExportOptions(rootComp, filepath)
    exportMgr.execute(options)
    return {"exported": filepath}


def _handle_import_mesh(params, design):
    filepath = params["filepath"]
    rootComp = design.rootComponent
    importMgr = app.importManager
    meshOptions = importMgr.createMeshImportOptions(filepath)
    unit_map = {
        "mm": adsk.core.DistanceUnits.MillimeterDistanceUnits,
        "cm": adsk.core.DistanceUnits.CentimeterDistanceUnits,
        "in": adsk.core.DistanceUnits.InchDistanceUnits,
    }
    meshOptions.units = unit_map.get(params.get("unit", "mm"), adsk.core.DistanceUnits.MillimeterDistanceUnits)
    importMgr.importToTarget2(meshOptions, rootComp)
    return {"imported": filepath}


# ---------------------------------------------------------------------------
# UTILITY HANDLERS
# ---------------------------------------------------------------------------


def _handle_undo(params, design):
    count = params.get("count", 1)
    for _ in range(count):
        app.executeTextCommand("Edit.Undo")
    return {"undone": count}


def _handle_delete_body(params, design):
    body = _get_body(params, design)
    name = body.name
    body.deleteMe()
    return {"deleted": name}


def _handle_delete_sketch(params, design):
    sketch = _get_sketch(params, design)
    name = sketch.name
    sketch.deleteMe()
    return {"deleted": name}


# ---------------------------------------------------------------------------
# EXECUTE PYTHON (escape hatch)
# ---------------------------------------------------------------------------


def _handle_execute_python(params, design):
    code = params.get("code")
    if not code:
        raise ValueError("'code' parameter required")

    session_id = params.get("session_id", "default")

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Build execution globals with Fusion context
    exec_globals = {
        "adsk": adsk,
        "app": app,
        "ui": ui,
        "design": design,
        "rootComp": design.rootComponent if design else None,
        "math": math,
        "json": json,
        "__builtins__": __builtins__,
    }

    # Restore session variables
    if session_id in _python_sessions:
        exec_globals.update(_python_sessions[session_id])

    try:
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            exec(compile(code, "<fusion-mcp>", "exec"), exec_globals)

        # Persist session variables (exclude standard keys)
        _reserved = {"adsk", "app", "ui", "design", "rootComp", "math", "json", "__builtins__", "__return__"}
        session_vars = {
            k: v for k, v in exec_globals.items()
            if k not in _reserved and not k.startswith("_") and not callable(v)
        }
        _python_sessions[session_id] = session_vars

        return_value = exec_globals.get("__return__")
        return {
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "return_value": str(return_value) if return_value is not None else None,
            "session_variables": list(session_vars.keys()),
        }
    except Exception as e:
        return {
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "error": str(e),
            "traceback": traceback.format_exc(),
        }


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_HANDLERS = {
    # Sketch
    "create_sketch": _handle_create_sketch,
    "finish_sketch": _handle_finish_sketch,
    "draw_line": _handle_draw_line,
    "draw_rectangle": _handle_draw_rectangle,
    "draw_circle": _handle_draw_circle,
    "draw_arc": _handle_draw_arc,
    "draw_polygon": _handle_draw_polygon,
    "draw_spline": _handle_draw_spline,
    "draw_slot": _handle_draw_slot,
    # 3D features
    "extrude": _handle_extrude,
    "revolve": _handle_revolve,
    "sweep": _handle_sweep,
    "loft": _handle_loft,
    # Modifications
    "fillet": _handle_fillet,
    "chamfer": _handle_chamfer,
    "shell": _handle_shell,
    "draft": _handle_draft,
    # Patterns
    "pattern_rectangular": _handle_pattern_rectangular,
    "pattern_circular": _handle_pattern_circular,
    "mirror": _handle_mirror,
    # Booleans
    "combine": _handle_combine,
    "split_body": _handle_split_body,
    # Components
    "create_component": _handle_create_component,
    "list_components": _handle_list_components,
    "delete_component": _handle_delete_component,
    "rename": _handle_rename,
    "move_component": _handle_move_component,
    "rotate_component": _handle_rotate_component,
    # Joints
    "create_joint": _handle_create_joint,
    "set_joint_limits": _handle_set_joint_limits,
    "drive_joint": _handle_drive_joint,
    "list_joints": _handle_list_joints,
    # Rigid groups
    "create_rigid_group": _handle_create_rigid_group,
    "list_rigid_groups": _handle_list_rigid_groups,
    "delete_rigid_group": _handle_delete_rigid_group,
    # Inspection
    "get_design_info": _handle_get_design_info,
    "get_body_info": _handle_get_body_info,
    "measure": _handle_measure,
    "check_interference": _handle_check_interference,
    "fit_view": _handle_fit_view,
    "screenshot": _handle_screenshot,
    # Export / Import
    "export_stl": _handle_export_stl,
    "export_step": _handle_export_step,
    "export_3mf": _handle_export_3mf,
    "import_mesh": _handle_import_mesh,
    # Utility
    "undo": _handle_undo,
    "delete_body": _handle_delete_body,
    "delete_sketch": _handle_delete_sketch,
    # Escape hatch
    "execute_python": _handle_execute_python,
}
