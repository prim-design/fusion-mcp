#!/usr/bin/env python3
"""
Fusion 360 MCP Server
=====================
Open-source MCP server for controlling Autodesk Fusion 360 from Claude.
Communicates with the Fusion 360 add-in over a TCP socket on localhost.

Tools cover: sketches, 3D features, modifications, patterns, booleans,
components, joints (all 7 types), rigid groups, inspection, export,
and an execute_python escape hatch for full API access.
"""

import base64
import json
import socket
import time
import uuid
from mcp.server.fastmcp import FastMCP, Image

HOST = "127.0.0.1"
PORT = 52361
TIMEOUT = 60  # seconds

mcp = FastMCP(
    "Fusion 360",
    instructions="""You are controlling Autodesk Fusion 360 through MCP tools.

CRITICAL RULES:
- All length units are in CENTIMETERS (cm). 1mm = 0.1cm, 10mm = 1.0cm.
- All angle units are in DEGREES (the add-in converts to radians internally).
- Coordinate system: X = right, Y = up, Z = toward you.
- Z-NEGATION GOTCHA: On XZ plane, sketch Y maps to -World Z. On YZ plane, sketch X maps to -World Z.
- Always call finish_sketch() after drawing operations before doing 3D features.
- Use get_design_info() to understand the current state before making changes.
- Use get_body_info() to find edge/face indices before fillet, chamfer, shell, or draft.
- Components must exist before creating joints between them.
- For joints, use as-built joints (default) when components are already positioned correctly.
- Use execute_python for any operation not covered by the curated tools.
""",
)


def send_command(method: str, params: dict) -> dict:
    """Send a command to the Fusion 360 add-in over TCP and wait for response."""
    request = json.dumps({
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }) + "\n"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect((HOST, PORT))
            sock.sendall(request.encode("utf-8"))

            # Read response (newline-delimited JSON)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break

            response = json.loads(data.decode("utf-8").strip())
            if not response.get("success"):
                raise Exception(response.get("error", "Unknown error from Fusion 360"))
            return response.get("result", {})

    except ConnectionRefusedError:
        raise Exception(
            "Cannot connect to Fusion 360. Is Fusion running with the FusionMCP add-in enabled? "
            f"(Expected TCP server at {HOST}:{PORT})"
        )
    except socket.timeout:
        raise Exception(f"Fusion 360 did not respond within {TIMEOUT}s")


# =============================================================================
# SKETCH CREATION
# =============================================================================


@mcp.tool()
def create_sketch(plane: str = "XY", offset: float = 0, component: str = None) -> dict:
    """
    Create a new sketch on a construction plane and enter edit mode.

    Args:
        plane: "XY" (horizontal), "XZ" (vertical front), or "YZ" (side)
        offset: Distance to offset the sketch plane from origin (cm). Default 0.
        component: Component name to create sketch in (default: root component)

    WARNING - Z-negation: On XZ plane, sketch +Y = world -Z. On YZ plane, sketch +X = world -Z.
    """
    return send_command("create_sketch", {"plane": plane, "offset": offset, "component": component})


@mcp.tool()
def finish_sketch() -> dict:
    """Exit sketch editing mode. MUST be called after drawing operations before 3D features."""
    return send_command("finish_sketch", {})


# =============================================================================
# SKETCH GEOMETRY
# =============================================================================


@mcp.tool()
def draw_line(x1: float, y1: float, x2: float, y2: float) -> dict:
    """Draw a straight line in the active sketch (cm)."""
    return send_command("draw_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})


@mcp.tool()
def draw_rectangle(x1: float, y1: float, x2: float, y2: float) -> dict:
    """Draw a rectangle from corner (x1,y1) to corner (x2,y2) in the active sketch (cm)."""
    return send_command("draw_rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})


@mcp.tool()
def draw_circle(center_x: float, center_y: float, radius: float) -> dict:
    """Draw a circle in the active sketch (cm)."""
    return send_command("draw_circle", {"center_x": center_x, "center_y": center_y, "radius": radius})


@mcp.tool()
def draw_arc(
    center_x: float, center_y: float,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
) -> dict:
    """Draw an arc by center + start + end points in the active sketch (cm)."""
    return send_command("draw_arc", {
        "center_x": center_x, "center_y": center_y,
        "start_x": start_x, "start_y": start_y,
        "end_x": end_x, "end_y": end_y,
    })


@mcp.tool()
def draw_polygon(center_x: float, center_y: float, radius: float, sides: int = 6) -> dict:
    """Draw a regular polygon (default hexagon) in the active sketch (cm)."""
    return send_command("draw_polygon", {
        "center_x": center_x, "center_y": center_y,
        "radius": radius, "sides": sides,
    })


@mcp.tool()
def draw_spline(points: list) -> dict:
    """
    Draw a spline through a list of points in the active sketch.

    Args:
        points: List of [x, y] coordinate pairs in cm. E.g. [[0,0], [1,2], [3,1]]
    """
    return send_command("draw_spline", {"points": points})


@mcp.tool()
def draw_slot(
    center1_x: float, center1_y: float,
    center2_x: float, center2_y: float,
    width: float,
) -> dict:
    """Draw a slot (stadium shape) between two center points with given width (cm)."""
    return send_command("draw_slot", {
        "center1_x": center1_x, "center1_y": center1_y,
        "center2_x": center2_x, "center2_y": center2_y,
        "width": width,
    })


# =============================================================================
# 3D FEATURES
# =============================================================================


@mcp.tool()
def extrude(
    distance: float,
    profile_index: int = -1,
    operation: str = "new",
    taper_angle: float = 0,
    component: str = None,
) -> dict:
    """
    Extrude a sketch profile into a 3D body.

    Args:
        distance: Extrusion distance in cm. Negative = opposite direction.
        profile_index: Which profile to extrude (-1 = last, 0 = first).
        operation: "new" (new body), "join", "cut", or "intersect".
        taper_angle: Draft angle during extrusion in degrees (0 = straight).
        component: Component name (default: root).
    """
    return send_command("extrude", {
        "distance": distance, "profile_index": profile_index,
        "operation": operation, "taper_angle": taper_angle,
        "component": component,
    })


@mcp.tool()
def revolve(angle: float = 360, axis: str = "Y", profile_index: int = -1, component: str = None) -> dict:
    """
    Revolve a sketch profile around an axis.

    Args:
        angle: Rotation angle in degrees (default 360 for full revolution).
        axis: Axis to revolve around — "X", "Y", or "Z" (default "Y").
        profile_index: Which profile (-1 = last).
        component: Component name (default: root).
    """
    return send_command("revolve", {
        "angle": angle, "axis": axis,
        "profile_index": profile_index, "component": component,
    })


@mcp.tool()
def sweep(profile_sketch: str, path_sketch: str, component: str = None) -> dict:
    """
    Sweep a profile along a path.

    Args:
        profile_sketch: Name of sketch containing the profile.
        path_sketch: Name of sketch containing the sweep path.
        component: Component name (default: root).
    """
    return send_command("sweep", {
        "profile_sketch": profile_sketch, "path_sketch": path_sketch,
        "component": component,
    })


@mcp.tool()
def loft(profile_sketches: list, component: str = None) -> dict:
    """
    Create a loft between two or more sketch profiles.

    Args:
        profile_sketches: List of sketch names containing profiles to loft between.
        component: Component name (default: root).
    """
    return send_command("loft", {"profile_sketches": profile_sketches, "component": component})


# =============================================================================
# MODIFICATIONS
# =============================================================================


@mcp.tool()
def fillet(radius: float, edges: list = None, body_index: int = -1) -> dict:
    """
    Round edges of a body.

    Args:
        radius: Fillet radius in cm.
        edges: List of edge indices (use get_body_info to find them). None = all edges.
        body_index: Which body (-1 = most recent).
    """
    return send_command("fillet", {"radius": radius, "edges": edges, "body_index": body_index})


@mcp.tool()
def chamfer(distance: float, edges: list = None, body_index: int = -1) -> dict:
    """
    Bevel edges of a body.

    Args:
        distance: Chamfer distance in cm.
        edges: List of edge indices. None = all edges.
        body_index: Which body (-1 = most recent).
    """
    return send_command("chamfer", {"distance": distance, "edges": edges, "body_index": body_index})


@mcp.tool()
def shell(thickness: float, faces_to_remove: list = None, body_index: int = -1) -> dict:
    """
    Hollow out a solid body.

    Args:
        thickness: Wall thickness in cm.
        faces_to_remove: Face indices to remove (creates openings). None = closed shell.
        body_index: Which body (-1 = most recent).
    """
    return send_command("shell", {
        "thickness": thickness, "faces_to_remove": faces_to_remove,
        "body_index": body_index,
    })


@mcp.tool()
def draft(
    angle: float,
    faces: list = None,
    body_index: int = -1,
    pull_direction: list = None,
) -> dict:
    """
    Apply draft angles to faces (for molding).

    Args:
        angle: Draft angle in degrees.
        faces: List of face indices. None = all vertical faces.
        body_index: Which body (-1 = most recent).
        pull_direction: [x, y, z] pull direction vector. Default [0, 0, 1].
    """
    return send_command("draft", {
        "angle": angle, "faces": faces, "body_index": body_index,
        "pull_direction": pull_direction or [0, 0, 1],
    })


# =============================================================================
# PATTERNS
# =============================================================================


@mcp.tool()
def pattern_rectangular(
    x_count: int, x_spacing: float,
    y_count: int = 1, y_spacing: float = 0,
    body_index: int = -1,
) -> dict:
    """
    Create a rectangular pattern of a body.

    Args:
        x_count: Instances in X direction.
        x_spacing: Spacing in X (cm).
        y_count: Instances in Y direction (default 1 = linear).
        y_spacing: Spacing in Y (cm).
        body_index: Which body (-1 = most recent).
    """
    return send_command("pattern_rectangular", {
        "x_count": x_count, "x_spacing": x_spacing,
        "y_count": y_count, "y_spacing": y_spacing,
        "body_index": body_index,
    })


@mcp.tool()
def pattern_circular(count: int, angle: float = 360, axis: str = "Z", body_index: int = -1) -> dict:
    """
    Create a circular pattern of a body.

    Args:
        count: Number of instances.
        angle: Total angle in degrees (360 = full circle).
        axis: "X", "Y", or "Z".
        body_index: Which body (-1 = most recent).
    """
    return send_command("pattern_circular", {
        "count": count, "angle": angle, "axis": axis, "body_index": body_index,
    })


@mcp.tool()
def mirror(plane: str = "YZ", body_index: int = -1) -> dict:
    """
    Mirror a body across a plane.

    Args:
        plane: "XY", "XZ", or "YZ" (default "YZ" for left-right symmetry).
        body_index: Which body (-1 = most recent).
    """
    return send_command("mirror", {"plane": plane, "body_index": body_index})


# =============================================================================
# BOOLEANS
# =============================================================================


@mcp.tool()
def combine(target_body: int, tool_bodies: list, operation: str = "cut", keep_tools: bool = False) -> dict:
    """
    Boolean operation between bodies.

    Args:
        target_body: Index of body to modify.
        tool_bodies: List of body indices to use as tools.
        operation: "cut" (subtract), "join" (add), or "intersect".
        keep_tools: Keep tool bodies after operation.
    """
    return send_command("combine", {
        "target_body": target_body, "tool_bodies": tool_bodies,
        "operation": operation, "keep_tools": keep_tools,
    })


@mcp.tool()
def split_body(body_index: int, split_tool: str, component: str = None) -> dict:
    """
    Split a body using a plane or face.

    Args:
        body_index: Body to split.
        split_tool: "XY", "XZ", "YZ", or a face reference.
        component: Component name (default: root).
    """
    return send_command("split_body", {
        "body_index": body_index, "split_tool": split_tool, "component": component,
    })


# =============================================================================
# COMPONENTS & ASSEMBLY
# =============================================================================


@mcp.tool()
def create_component(name: str = None, from_body: int = -1) -> dict:
    """
    Create a new component from a body.

    Args:
        name: Component name.
        from_body: Body index to convert (-1 = most recent body).
    """
    return send_command("create_component", {"name": name, "from_body": from_body})


@mcp.tool()
def list_components() -> dict:
    """List all components with names, positions, and body counts."""
    return send_command("list_components", {})


@mcp.tool()
def delete_component(name: str = None, index: int = None) -> dict:
    """Delete a component by name or index."""
    return send_command("delete_component", {"name": name, "index": index})


@mcp.tool()
def rename(target: str, new_name: str, target_type: str = "component", index: int = None) -> dict:
    """
    Rename a component, body, sketch, or joint.

    Args:
        target: Current name or index of the item to rename.
        new_name: The new name.
        target_type: What to rename — "component", "body", "sketch", or "joint".
        index: Use index instead of name to identify the target.
    """
    return send_command("rename", {
        "target": target, "new_name": new_name,
        "target_type": target_type, "index": index,
    })


@mcp.tool()
def move_component(
    x: float = 0, y: float = 0, z: float = 0,
    name: str = None, index: int = None,
    absolute: bool = True,
) -> dict:
    """
    Move a component.

    Args:
        x, y, z: Position (absolute) or offset (relative) in cm.
        name: Component name.
        index: Component index (alternative to name).
        absolute: True = set position, False = move by offset.
    """
    return send_command("move_component", {
        "x": x, "y": y, "z": z,
        "name": name, "index": index, "absolute": absolute,
    })


@mcp.tool()
def rotate_component(
    angle: float,
    axis: str = "Z",
    name: str = None, index: int = None,
    origin_x: float = 0, origin_y: float = 0, origin_z: float = 0,
) -> dict:
    """
    Rotate a component.

    Args:
        angle: Rotation in degrees.
        axis: "X", "Y", or "Z".
        name: Component name.
        index: Component index.
        origin_x/y/z: Center of rotation (cm).
    """
    return send_command("rotate_component", {
        "angle": angle, "axis": axis,
        "name": name, "index": index,
        "origin_x": origin_x, "origin_y": origin_y, "origin_z": origin_z,
    })


# =============================================================================
# JOINTS
# =============================================================================


@mcp.tool()
def create_joint(
    occurrence1: str,
    occurrence2: str,
    joint_type: str = "rigid",
    axis: str = "Z",
    angle: float = 0,
    offset: float = 0,
    flip: bool = False,
) -> dict:
    """
    Create an as-built joint between two components (components stay in place).

    Args:
        occurrence1: Name or index of first component.
        occurrence2: Name or index of second component.
        joint_type: One of: rigid, revolute, slider, cylindrical, pin_slot, planar, ball.
        axis: Primary joint axis — "X", "Y", or "Z".
        angle: Initial rotation value in degrees (for revolute/cylindrical).
        offset: Initial slide value in cm (for slider/cylindrical).
        flip: Flip the joint direction.
    """
    return send_command("create_joint", {
        "occurrence1": occurrence1, "occurrence2": occurrence2,
        "joint_type": joint_type, "axis": axis,
        "angle": angle, "offset": offset, "flip": flip,
    })


@mcp.tool()
def set_joint_limits(
    joint: str,
    min_angle: float = None, max_angle: float = None,
    min_distance: float = None, max_distance: float = None,
    rest_angle: float = None, rest_distance: float = None,
) -> dict:
    """
    Set limits on a joint.

    Args:
        joint: Joint name or index.
        min_angle/max_angle: Rotation limits in degrees (revolute, cylindrical, pin_slot, planar, ball).
        min_distance/max_distance: Slide limits in cm (slider, cylindrical, pin_slot).
        rest_angle/rest_distance: Rest/home values.
    """
    return send_command("set_joint_limits", {
        "joint": joint,
        "min_angle": min_angle, "max_angle": max_angle,
        "min_distance": min_distance, "max_distance": max_distance,
        "rest_angle": rest_angle, "rest_distance": rest_distance,
    })


@mcp.tool()
def drive_joint(joint: str, angle: float = None, distance: float = None) -> dict:
    """
    Drive a joint to a specific position.

    Args:
        joint: Joint name or index.
        angle: Target angle in degrees (revolute/cylindrical).
        distance: Target distance in cm (slider/cylindrical).
    """
    return send_command("drive_joint", {"joint": joint, "angle": angle, "distance": distance})


@mcp.tool()
def list_joints() -> dict:
    """List all joints with their types, components, and current values."""
    return send_command("list_joints", {})


# =============================================================================
# RIGID GROUPS
# =============================================================================


@mcp.tool()
def create_rigid_group(occurrences: list, name: str = None) -> dict:
    """
    Lock multiple components together so they move as one.

    Args:
        occurrences: List of component names or indices.
        name: Optional name for the rigid group.
    """
    return send_command("create_rigid_group", {"occurrences": occurrences, "name": name})


@mcp.tool()
def list_rigid_groups() -> dict:
    """List all rigid groups and their member components."""
    return send_command("list_rigid_groups", {})


@mcp.tool()
def delete_rigid_group(name: str = None, index: int = None) -> dict:
    """Delete a rigid group by name or index."""
    return send_command("delete_rigid_group", {"name": name, "index": index})


# =============================================================================
# INSPECTION
# =============================================================================


@mcp.tool()
def get_design_info() -> dict:
    """Get current design state: name, body count, sketch count, component count, active sketch, joints."""
    return send_command("get_design_info", {})


@mcp.tool()
def get_body_info(body_index: int = -1, component: str = None) -> dict:
    """
    Get detailed body info including edges and faces with indices.
    Use this before fillet, chamfer, shell, or draft to find the right indices.

    Args:
        body_index: Which body (-1 = most recent).
        component: Component name (default: root).
    """
    return send_command("get_body_info", {"body_index": body_index, "component": component})


@mcp.tool()
def measure(target: str = "body", body_index: int = -1, edge_index: int = None, face_index: int = None) -> dict:
    """
    Measure dimensions.

    Args:
        target: "body" (volume, area, bbox), "edge" (length), or "face" (area).
        body_index: Which body (-1 = most recent).
        edge_index: Edge index (for target="edge").
        face_index: Face index (for target="face").
    """
    return send_command("measure", {
        "target": target, "body_index": body_index,
        "edge_index": edge_index, "face_index": face_index,
    })


@mcp.tool()
def check_interference() -> dict:
    """Check for overlapping components (bounding box collision detection)."""
    return send_command("check_interference", {})


@mcp.tool()
def screenshot(
    width: int = 1920,
    height: int = 1080,
    view: str = None,
    eye_x: float = None, eye_y: float = None, eye_z: float = None,
    target_x: float = None, target_y: float = None, target_z: float = None,
    fit: bool = True,
) -> Image:
    """
    Capture a screenshot of the current Fusion 360 viewport from any angle.
    Returns the image directly so you can see the current state of the design.

    Args:
        width: Image width in pixels (default 1920).
        height: Image height in pixels (default 1080).
        view: Preset view — "front", "back", "top", "bottom", "left", "right", "iso", "iso_back".
              If None, uses the current camera angle.
        eye_x/y/z: Custom camera eye position (cm). Overrides view preset.
        target_x/y/z: Custom camera target/look-at position (cm). Default (0,0,0).
        fit: Fit all geometry in view after setting angle (default True).
    """
    result = send_command("screenshot", {
        "width": width, "height": height,
        "view": view,
        "eye_x": eye_x, "eye_y": eye_y, "eye_z": eye_z,
        "target_x": target_x, "target_y": target_y, "target_z": target_z,
        "fit": fit,
    })
    image_data = base64.b64decode(result["image_base64"])
    return Image(data=image_data, format="png")


@mcp.tool()
def fit_view() -> dict:
    """Zoom to fit all geometry in the viewport."""
    return send_command("fit_view", {})


# =============================================================================
# EXPORT / IMPORT
# =============================================================================


@mcp.tool()
def export_stl(filepath: str) -> dict:
    """Export design as STL (3D printing)."""
    return send_command("export_stl", {"filepath": filepath})


@mcp.tool()
def export_step(filepath: str) -> dict:
    """Export design as STEP (CAD interchange)."""
    return send_command("export_step", {"filepath": filepath})


@mcp.tool()
def export_3mf(filepath: str) -> dict:
    """Export design as 3MF (modern 3D printing format)."""
    return send_command("export_3mf", {"filepath": filepath})


@mcp.tool()
def import_mesh(filepath: str, unit: str = "mm") -> dict:
    """Import STL/OBJ/3MF mesh file. Unit: mm, cm, or in."""
    return send_command("import_mesh", {"filepath": filepath, "unit": unit})


# =============================================================================
# UTILITY
# =============================================================================


@mcp.tool()
def undo(count: int = 1) -> dict:
    """Undo recent operations."""
    return send_command("undo", {"count": count})


@mcp.tool()
def delete_body(body_index: int = -1) -> dict:
    """Delete a body by index (-1 = most recent)."""
    return send_command("delete_body", {"body_index": body_index})


@mcp.tool()
def delete_sketch(sketch_index: int = -1) -> dict:
    """Delete a sketch by index (-1 = most recent)."""
    return send_command("delete_sketch", {"sketch_index": sketch_index})


@mcp.tool()
def batch(commands: list) -> dict:
    """
    Execute multiple commands in one round-trip for speed.

    Args:
        commands: List of {"method": "tool_name", "params": {...}} dicts.

    Example:
        batch([
            {"method": "create_sketch", "params": {"plane": "XY"}},
            {"method": "draw_rectangle", "params": {"x1": -5, "y1": -5, "x2": 5, "y2": 5}},
            {"method": "finish_sketch", "params": {}},
            {"method": "extrude", "params": {"distance": 3}}
        ])
    """
    return send_command("batch", {"commands": commands})


# =============================================================================
# ESCAPE HATCH — EXECUTE ARBITRARY PYTHON IN FUSION
# =============================================================================


@mcp.tool()
def execute_python(code: str, session_id: str = "default") -> dict:
    """
    Execute arbitrary Python code inside Fusion 360. Use this for operations
    not covered by the curated tools.

    The code runs with full access to:
    - adsk.core, adsk.fusion, adsk.cam
    - app (Application), ui (UserInterface), design (activeProduct), rootComp (rootComponent)
    - All Fusion 360 API classes and methods

    Set __return__ to pass a value back. Use print() for debug output.

    Args:
        code: Python code to execute.
        session_id: Session ID for persisting variables across calls.

    Example:
        execute_python('''
            # Create a construction axis
            axes = rootComp.constructionAxes
            axisInput = axes.createInput()
            axisInput.setByTwoPoints(
                rootComp.originConstructionPoint,
                adsk.core.Point3D.create(10, 10, 0)
            )
            axis = axes.add(axisInput)
            __return__ = axis.name
        ''')
    """
    return send_command("execute_python", {"code": code, "session_id": session_id})


# =============================================================================
# MAIN
# =============================================================================


def main():
    mcp.run()


if __name__ == "__main__":
    main()
