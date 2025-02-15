# mypy: disable-error-code="misc"
#
# TLiteralString overloads are waiting on PEP 675 support in mypy.
# https://github.com/python/mypy/issues/12554
#
# In the meantime, it works great in Pyright/Pylance!

from __future__ import annotations

import abc
import base64
import colorsys
import contextlib
import io
import mimetypes
import queue
import threading
import time
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
    cast,
)

import imageio.v3 as iio
import numpy as onp
import numpy.typing as onpt
import trimesh
import trimesh.creation
import trimesh.exchange
import trimesh.visual
from typing_extensions import Literal, ParamSpec, TypeAlias, assert_never

from . import _messages, infra, theme
from ._scene_handles import (
    CameraFrustumHandle,
    FrameHandle,
    GlbHandle,
    Gui3dContainerHandle,
    ImageHandle,
    LabelHandle,
    MeshHandle,
    PointCloudHandle,
    SceneNodeHandle,
    SceneNodePointerEvent,
    ScenePointerEvent,
    TransformControlsHandle,
    _TransformControlsState,
)

if TYPE_CHECKING:
    from ._viser import ClientHandle
    from .infra import ClientId


P = ParamSpec("P")


def _hex_from_hls(h: float, l: float, s: float) -> str:
    """Converts HLS values in [0.0, 1.0] to a hex-formatted string, eg 0xffffff."""
    return "#" + "".join(
        [
            int(min(255, max(0, channel * 255.0)) + 0.5).to_bytes(1, "little").hex()
            for channel in colorsys.hls_to_rgb(h, l, s)
        ]
    )


def _colors_to_uint8(colors: onp.ndarray) -> onpt.NDArray[onp.uint8]:
    """Convert intensity values to uint8. We assume the range [0,1] for floats, and
    [0,255] for integers. Accepts any shape."""
    if colors.dtype != onp.uint8:
        if onp.issubdtype(colors.dtype, onp.floating):
            colors = onp.clip(colors * 255.0, 0, 255).astype(onp.uint8)
        if onp.issubdtype(colors.dtype, onp.integer):
            colors = onp.clip(colors, 0, 255).astype(onp.uint8)
    return colors


RgbTupleOrArray: TypeAlias = Union[
    Tuple[int, int, int], Tuple[float, float, float], onp.ndarray
]


def _encode_rgb(rgb: RgbTupleOrArray) -> int:
    if isinstance(rgb, onp.ndarray):
        assert rgb.shape == (3,)
    rgb_fixed = tuple(
        value if onp.issubdtype(type(value), onp.integer) else int(value * 255)
        for value in rgb
    )
    assert len(rgb_fixed) == 3
    return int(rgb_fixed[0] * (256**2) + rgb_fixed[1] * 256 + rgb_fixed[2])


def _encode_image_base64(
    image: onp.ndarray,
    format: Literal["png", "jpeg"],
    jpeg_quality: Optional[int] = None,
) -> Tuple[Literal["image/png", "image/jpeg"], str]:
    media_type: Literal["image/png", "image/jpeg"]
    image = _colors_to_uint8(image)
    with io.BytesIO() as data_buffer:
        if format == "png":
            media_type = "image/png"
            iio.imwrite(data_buffer, image, extension=".png")
        elif format == "jpeg":
            media_type = "image/jpeg"
            iio.imwrite(
                data_buffer,
                image[..., :3],  # Strip alpha.
                extension=".jpeg",
                quality=75 if jpeg_quality is None else jpeg_quality,
            )
        else:
            assert_never(format)

        base64_data = base64.b64encode(data_buffer.getvalue()).decode("ascii")

    return media_type, base64_data


TVector = TypeVar("TVector", bound=tuple)


def cast_vector(vector: TVector | onp.ndarray, length: int) -> TVector:
    if not isinstance(vector, tuple):
        assert cast(onp.ndarray, vector).shape == (length,)
    return cast(TVector, tuple(map(float, vector)))


class MessageApi(abc.ABC):
    """Interface for all commands we can use to send messages over a websocket connection.

    Should be implemented by both our global server object (for broadcasting) and by
    invidividual clients."""

    _locked_thread_id: int  # Appeasing mypy 1.5.1, not sure why this is needed.

    def __init__(self, handler: infra.MessageHandler) -> None:
        self._message_handler = handler

        super().__init__()

        self._handle_from_transform_controls_name: Dict[
            str, TransformControlsHandle
        ] = {}
        self._handle_from_node_name: Dict[str, SceneNodeHandle] = {}

        # Callbacks for scene pointer events -- by default don't enable them.
        self._scene_pointer_cb: List[Callable[[ScenePointerEvent], None]] = []
        self._scene_pointer_enabled = False

        handler.register_handler(
            _messages.TransformControlsUpdateMessage,
            self._handle_transform_controls_updates,
        )
        handler.register_handler(
            _messages.SceneNodeClickMessage,
            self._handle_node_click_updates,
        )
        handler.register_handler(
            _messages.ScenePointerMessage,
            self._handle_scene_pointer_updates,
        )

        self._atomic_lock = threading.Lock()
        self._queued_messages: queue.Queue = queue.Queue()
        self._locked_thread_id = -1

    def configure_theme(
        self,
        *,
        titlebar_content: Optional[theme.TitlebarConfig] = None,
        control_layout: Literal["floating", "collapsible", "fixed"] = "floating",
        control_width: Literal["small", "medium", "large"] = "medium",
        dark_mode: bool = False,
        show_logo: bool = True,
        brand_color: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """Configures the visual appearance of the viser front-end.

        Args:
            titlebar_content: Optional configuration for the title bar.
            control_layout: The layout of control elements, options are "floating",
                            "collapsible", or "fixed".
            control_width: The width of control elements, options are "small",
                           "medium", or "large".
            dark_mode: A boolean indicating if dark mode should be enabled.
            show_logo: A boolean indicating if the logo should be displayed.
            brand_color: An optional tuple of integers (RGB) representing the brand color.
        """

        colors_cast: Optional[
            Tuple[str, str, str, str, str, str, str, str, str, str]
        ] = None

        if brand_color is not None:
            assert len(brand_color) in (3, 10)
            if len(brand_color) == 3:
                assert all(
                    map(lambda val: isinstance(val, int), brand_color)
                ), "All channels should be integers."

                # RGB => HLS.
                h, l, s = colorsys.rgb_to_hls(
                    brand_color[0] / 255.0,
                    brand_color[1] / 255.0,
                    brand_color[2] / 255.0,
                )

                # Automatically generate a 10-color palette.
                min_l = max(l - 0.08, 0.0)
                max_l = min(0.8 + 0.5, 0.9)
                l = max(min_l, min(max_l, l))

                primary_index = 8
                ls = tuple(
                    onp.interp(
                        x=onp.arange(10),
                        xp=onp.array([0, primary_index, 9]),
                        fp=onp.array([max_l, l, min_l]),
                    )
                )
                colors_cast = tuple(_hex_from_hls(h, ls[i], s) for i in range(10))  # type: ignore

        assert colors_cast is None or all(
            [isinstance(val, str) and val.startswith("#") for val in colors_cast]
        ), "All string colors should be in hexadecimal + prefixed with #, eg #ffffff."

        self._queue(
            _messages.ThemeConfigurationMessage(
                titlebar_content=titlebar_content,
                control_layout=control_layout,
                control_width=control_width,
                dark_mode=dark_mode,
                show_logo=show_logo,
                colors=colors_cast,
            ),
        )

    def set_up_direction(
        self,
        direction: Literal["+x", "+y", "+z", "-x", "-y", "-z"]
        | Tuple[float, float, float]
        | onp.ndarray,
    ) -> None:
        """Set the global up direction of the scene. By default we follow +Z-up
        (similar to Blender, 3DS Max, ROS, etc), the most common alternative is
        +Y (OpenGL, Maya, etc).

        Args:
            direction: New up direction. Can either be a string (one of +x, +y,
                +z, -x, -y, -z) or a length-3 direction vector.
        """
        if isinstance(direction, str):
            direction = {
                "+x": (1, 0, 0),
                "+y": (0, 1, 0),
                "+z": (0, 0, 1),
                "-x": (-1, 0, 0),
                "-y": (0, -1, 0),
                "-z": (0, 0, -1),
            }[direction]
        assert not isinstance(direction, str)
        self._queue(_messages.SetUpDirectionMessage(cast_vector(direction, 3)))

    def add_glb(
        self,
        name: str,
        glb_data: bytes,
        scale=1.0,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> GlbHandle:
        """Add a general 3D asset via binary glTF (GLB).

        To load glTF files from disk, you can convert to GLB via a library like
        `pygltflib`.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
              define a kinematic tree.
            glb_data: A binary payload.
            scale: A scale for resizing the GLB asset.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        self._queue(_messages.GlbMessage(name, glb_data, scale))
        return GlbHandle._make(self, name, wxyz, position, visible)

    def add_spline_catmull_rom(
        self,
        name: str,
        positions: Tuple[Tuple[float, float, float], ...] | onp.ndarray,
        curve_type: Literal["centripetal", "chordal", "catmullrom"] = "centripetal",
        tension: float = 0.5,
        closed: bool = False,
        line_width: float = 1,
        color: RgbTupleOrArray = (20, 20, 20),
        segments: Optional[int] = None,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> SceneNodeHandle:
        """Add a spline to the scene using Catmull-Rom interpolation.

        This method creates a spline based on a set of positions and interpolates
        them using the Catmull-Rom algorithm. This can be used to create smooth curves.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            positions: A tuple of 3D positions (x, y, z) defining the spline's path.
            curve_type: Type of the curve ('centripetal', 'chordal', 'catmullrom').
            tension: Tension of the curve. Affects the tightness of the curve.
            closed: Boolean indicating if the spline is closed (forms a loop).
            line_width: Width of the spline line.
            color: Color of the spline as an RGB tuple.
            segments: Number of segments to divide the spline into.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        if isinstance(positions, onp.ndarray):
            assert len(positions.shape) == 2 and positions.shape[1] == 3
            positions = tuple(map(tuple, positions))  # type: ignore
        assert len(positions[0]) == 3
        assert isinstance(positions, tuple)
        self._queue(
            _messages.CatmullRomSplineMessage(
                name,
                positions,
                curve_type,
                tension,
                closed,
                line_width,
                _encode_rgb(color),
                segments=segments,
            )
        )
        return SceneNodeHandle._make(self, name, wxyz, position, visible)

    def add_spline_cubic_bezier(
        self,
        name: str,
        positions: Tuple[Tuple[float, float, float], ...] | onp.ndarray,
        control_points: Tuple[Tuple[float, float, float], ...] | onp.ndarray,
        line_width: float = 1,
        color: RgbTupleOrArray = (20, 20, 20),
        segments: Optional[int] = None,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> SceneNodeHandle:
        """Add a spline to the scene using Cubic Bezier interpolation.

        This method allows for the creation of a cubic Bezier spline based on given
        positions and control points. It is useful for creating complex, smooth,
        curving shapes.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            positions: A tuple of 3D positions (x, y, z) defining the spline's key points.
            control_points: A tuple of control points for Bezier curve shaping.
            line_width: Width of the spline line.
            color: Color of the spline as an RGB tuple.
            segments: Number of segments to divide the spline into.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """

        if isinstance(positions, onp.ndarray):
            assert len(positions.shape) == 2 and positions.shape[1] == 3
            positions = tuple(map(tuple, positions))  # type: ignore
        if isinstance(control_points, onp.ndarray):
            assert len(control_points.shape) == 2 and control_points.shape[1] == 3
            control_points = tuple(map(tuple, control_points))  # type: ignore

        assert isinstance(positions, tuple)
        assert isinstance(control_points, tuple)
        assert len(control_points) == (2 * len(positions) - 2)
        self._queue(
            _messages.CubicBezierSplineMessage(
                name,
                positions,
                control_points,
                line_width,
                _encode_rgb(color),
                segments=segments,
            )
        )
        return SceneNodeHandle._make(self, name, wxyz, position, visible)

    def add_camera_frustum(
        self,
        name: str,
        fov: float,
        aspect: float,
        scale: float = 0.3,
        color: RgbTupleOrArray = (20, 20, 20),
        image: Optional[onp.ndarray] = None,
        format: Literal["png", "jpeg"] = "jpeg",
        jpeg_quality: Optional[int] = None,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> CameraFrustumHandle:
        """Add a camera frustum to the scene for visualization.

        This method adds a frustum representation, typically used to visualize the
        field of view of a camera. It's helpful for understanding the perspective
        and coverage of a camera in the 3D space.

        Like all cameras in the viser Python API, frustums follow the OpenCV [+Z forward,
        +X right, +Y down] convention. fov is vertical in radians; aspect is width over height

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            fov: Field of view of the camera (in radians).
            aspect: Aspect ratio of the camera (width over height).
            scale: Scale factor for the size of the frustum.
            color: Color of the frustum as an RGB tuple.
            image: Optional image to be displayed on the frustum.
            format: Format of the provided image ('png' or 'jpeg').
            jpeg_quality: Quality of the jpeg image (if jpeg format is used).
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """

        if image is not None:
            media_type, base64_data = _encode_image_base64(
                image, format, jpeg_quality=jpeg_quality
            )
        else:
            media_type = None
            base64_data = None

        self._queue(
            _messages.CameraFrustumMessage(
                name=name,
                fov=fov,
                aspect=aspect,
                scale=scale,
                # (255, 255, 255) => 0xffffff, etc
                color=_encode_rgb(color),
                image_media_type=media_type,
                image_base64_data=base64_data,
            )
        )
        return CameraFrustumHandle._make(self, name, wxyz, position, visible)

    def add_frame(
        self,
        name: str,
        show_axes: bool = True,
        axes_length: float = 0.5,
        axes_radius: float = 0.025,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> FrameHandle:
        """Add a coordinate frame to the scene.

        This method is used for adding a visual representation of a coordinate frame,
        which can help in understanding the orientation and position of objects in 3D space.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            show_axes: Boolean to indicate whether to show the axes.
            axes_length: Length of each axis.
            axes_radius: Radius of each axis.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        self._queue(
            _messages.FrameMessage(
                name=name,
                show_axes=show_axes,
                axes_length=axes_length,
                axes_radius=axes_radius,
            )
        )
        return FrameHandle._make(self, name, wxyz, position, visible)

    def add_grid(
        self,
        name: str,
        width: float = 10.0,
        height: float = 10.0,
        width_segments: int = 10,
        height_segments: int = 10,
        plane: Literal["xz", "xy", "yx", "yz", "zx", "zy"] = "xy",
        cell_color: RgbTupleOrArray = (200, 200, 200),
        cell_thickness: float = 1.0,
        cell_size: float = 0.5,
        section_color: RgbTupleOrArray = (140, 140, 140),
        section_thickness: float = 1.0,
        section_size: float = 1.0,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> SceneNodeHandle:
        """Add a grid to the scene.

        This method creates a grid which can be used as a reference for scaling and
        positioning objects in the scene. It's particularly useful for providing a sense
        of scale and orientation.

        Args:
            name: Name of the grid.
            width: Width of the grid.
            height: Height of the grid.
            width_segments: Number of segments along the width.
            height_segments: Number of segments along the height.
            plane: The plane in which the grid is oriented (e.g., 'xy', 'yz').
            cell_color: Color of the grid cells as an RGB tuple.
            cell_thickness: Thickness of the grid lines.
            cell_size: Size of each cell in the grid.
            section_color: Color of the grid sections as an RGB tuple.
            section_thickness: Thickness of the section lines.
            section_size: Size of each section in the grid.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        self._queue(
            _messages.GridMessage(
                name=name,
                width=width,
                height=height,
                width_segments=width_segments,
                height_segments=height_segments,
                plane=plane,
                cell_color=_encode_rgb(cell_color),
                cell_thickness=cell_thickness,
                cell_size=cell_size,
                section_color=_encode_rgb(section_color),
                section_thickness=section_thickness,
                section_size=section_size,
            )
        )
        return SceneNodeHandle._make(self, name, wxyz, position, visible)

    def add_label(
        self,
        name: str,
        text: str,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> LabelHandle:
        """Add a 2D label to the scene.

        This method creates a text label in the 3D scene, which can be used to annotate
        or provide information about specific points or objects.

        Args:
            name: Name of the label.
            text: Text content of the label.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        self._queue(_messages.LabelMessage(name, text))
        return LabelHandle._make(self, name, wxyz, position, visible=visible)

    def add_point_cloud(
        self,
        name: str,
        points: onp.ndarray,
        colors: onp.ndarray | Tuple[float, float, float],
        point_size: float = 0.1,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> PointCloudHandle:
        """Add a point cloud to the scene.

        Args:
            name: Name of scene node. Determines location in kinematic tree.
            points: Location of points. Should have shape (N, 3).
            colors: Colors of points. Should have shape (N, 3) or (3,).
            point_size: Size of each point.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        colors_cast = _colors_to_uint8(onp.asarray(colors))
        assert (
            len(points.shape) == 2 and points.shape[-1] == 3
        ), "Shape of points should be (N, 3)."
        assert colors_cast.shape == points.shape or colors_cast.shape == (
            3,
        ), "Shape of colors should be (N, 3) or (3,)."

        if colors_cast.shape == (3,):
            colors_cast = onp.tile(colors_cast[None, :], reps=(points.shape[0], 1))

        self._queue(
            _messages.PointCloudMessage(
                name=name,
                points=points.astype(onp.float32),
                colors=colors_cast,
                point_size=point_size,
            )
        )
        return PointCloudHandle._make(self, name, wxyz, position, visible)

    def add_mesh(self, *args, **kwargs) -> MeshHandle:
        """Deprecated alias for `add_mesh_simple()`."""
        return self.add_mesh_simple(*args, **kwargs)

    def add_mesh_simple(
        self,
        name: str,
        vertices: onp.ndarray,
        faces: onp.ndarray,
        color: RgbTupleOrArray = (90, 200, 255),
        wireframe: bool = False,
        opacity: Optional[float] = None,
        material: Literal["standard", "toon3", "toon5"] = "standard",
        flat_shading: bool = True,
        side: Literal["front", "back", "double"] = "front",
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> MeshHandle:
        """Add a mesh to the scene.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            vertices: A numpy array of vertex positions. Should have shape (V, 3).
            faces: A numpy array of faces, where each face is represented by indices of
                vertices. Should have shape (F,)
            color: Color of the mesh as an RGB tuple.
            wireframe: Boolean indicating if the mesh should be rendered as a wireframe.
            opacity: Opacity of the mesh. None means opaque.
            material: Material type of the mesh ('standard', 'toon3', 'toon5').
            flat_shading: Whether to do flat shading. Set to False to apply smooth
                shading.
            side: Side of the surface to render ('front', 'back', 'double').
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation from parent frame to local frame (t_pl).
            visible: Whether or not this mesh is initially visible.

        Returns:
            Handle for manipulating scene node.
        """

        self._queue(
            _messages.MeshMessage(
                name,
                vertices.astype(onp.float32),
                faces.astype(onp.uint32),
                # (255, 255, 255) => 0xffffff, etc
                color=_encode_rgb(color),
                vertex_colors=None,
                wireframe=wireframe,
                opacity=opacity,
                flat_shading=flat_shading,
                side=side,
                material=material,
            )
        )
        node_handle = MeshHandle._make(self, name, wxyz, position, visible)
        return node_handle

    def add_mesh_trimesh(
        self,
        name: str,
        mesh: trimesh.Trimesh,
        scale: float = 1.0,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> GlbHandle:
        """Add a trimesh mesh to the scene. Internally calls `self.add_glb()`.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
              define a kinematic tree.
            mesh: A trimesh mesh object.
            scale: A scale for resizing the mesh.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node.
        """

        with io.BytesIO() as data_buffer:
            mesh.export(data_buffer, file_type="glb")
            glb_data = data_buffer.getvalue()
            return self.add_glb(
                name,
                glb_data=glb_data,
                scale=scale,
                wxyz=wxyz,
                position=position,
                visible=visible,
            )

    def add_box(
        self,
        name: str,
        color: RgbTupleOrArray,
        dimensions: Tuple[float, float, float] | onp.ndarray = (1.0, 1.0, 1.0),
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> MeshHandle:
        """Add a box to the scene.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            color: Color of the box as an RGB tuple.
            dimensions: Dimensions of the box (x, y, z).
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation from parent frame to local frame (t_pl).
            visible: Whether or not this box is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        mesh = trimesh.creation.box(dimensions)

        return self.add_mesh_simple(
            name=name,
            vertices=mesh.vertices,
            faces=mesh.faces,
            color=color,
            flat_shading=True,
            position=position,
            wxyz=wxyz,
            visible=visible,
        )

    def add_icosphere(
        self,
        name: str,
        radius: float,
        color: RgbTupleOrArray,
        subdivisions: int = 3,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> MeshHandle:
        """Add an icosphere to the scene.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            radius: Radius of the icosphere.
            color: Color of the icosphere as an RGB tuple.
            subdivisions: Number of subdivisions to use when creating the icosphere.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation from parent frame to local frame (t_pl).
            visible: Whether or not this icosphere is initially visible.

        Returns:
            Handle for manipulating scene node.
        """
        mesh = trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)

        # We use add_mesh_simple() because it lets us do smooth shading;
        # add_mesh_trimesh() currently does not.
        return self.add_mesh_simple(
            name=name,
            vertices=mesh.vertices,
            faces=mesh.faces,
            color=color,
            flat_shading=False,
            position=position,
            wxyz=wxyz,
            visible=visible,
        )

    def set_background_image(
        self,
        image: onp.ndarray,
        format: Literal["png", "jpeg"] = "jpeg",
        jpeg_quality: Optional[int] = None,
        depth: Optional[onp.ndarray] = None,
    ) -> None:
        """Set a background image for the scene, optionally with depth compositing.

        Args:
            image: The image to set as the background. Should have shape (H, W, 3).
            format: Format to transport and display the image using ('png' or 'jpeg').
            jpeg_quality: Quality of the jpeg image (if jpeg format is used).
            depth: Optional depth image to use to composite background with scene elements.
        """
        media_type, base64_data = _encode_image_base64(
            image, format, jpeg_quality=jpeg_quality
        )

        # Encode depth if provided. We use a 3-channel PNG to represent a fixed point
        # depth at each pixel.
        depth_base64data = None
        if depth is not None:
            # Convert to fixed-point.
            # We'll support from 0 -> (2^24 - 1) / 100_000.
            #
            # This translates to a range of [0, 167.77215], with a precision of 1e-5.
            assert len(depth.shape) == 2 or (
                len(depth.shape) == 3 and depth.shape[2] == 1
            ), "Depth should have shape (H,W) or (H,W,1)."
            depth = onp.clip(depth * 100_000, 0, 2**24 - 1).astype(onp.uint32)
            assert depth is not None  # Appease mypy.
            intdepth: onp.ndarray = depth.reshape((*depth.shape[:2], 1)).view(onp.uint8)
            assert intdepth.shape == (*depth.shape[:2], 4)
            with io.BytesIO() as data_buffer:
                iio.imwrite(data_buffer, intdepth[:, :, :3], extension=".png")
                depth_base64data = base64.b64encode(data_buffer.getvalue()).decode(
                    "ascii"
                )

        self._queue(
            _messages.BackgroundImageMessage(
                media_type=media_type,
                base64_rgb=base64_data,
                base64_depth=depth_base64data,
            )
        )

    def add_image(
        self,
        name: str,
        image: onp.ndarray,
        render_width: float,
        render_height: float,
        format: Literal["png", "jpeg"] = "jpeg",
        jpeg_quality: Optional[int] = None,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> ImageHandle:
        """Add a 2D image to the scene.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            image: A numpy array representing the image.
            render_width: Width at which the image should be rendered in the scene.
            render_height: Height at which the image should be rendered in the scene.
            format: Format to transport and display the image using ('png' or 'jpeg').
            jpeg_quality: Quality of the jpeg image (if jpeg format is used).
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation from parent frame to local frame (t_pl).
            visible: Whether or not this image is initially visible.

        Returns:
            Handle for manipulating scene node.
        """

        media_type, base64_data = _encode_image_base64(
            image, format, jpeg_quality=jpeg_quality
        )
        self._queue(
            _messages.ImageMessage(
                name=name,
                media_type=media_type,
                base64_data=base64_data,
                render_width=render_width,
                render_height=render_height,
            )
        )
        return ImageHandle._make(self, name, wxyz, position, visible)

    def add_transform_controls(
        self,
        name: str,
        scale: float = 1.0,
        line_width: float = 2.5,
        fixed: bool = False,
        auto_transform: bool = True,
        active_axes: Tuple[bool, bool, bool] = (True, True, True),
        disable_axes: bool = False,
        disable_sliders: bool = False,
        disable_rotations: bool = False,
        translation_limits: Tuple[
            Tuple[float, float], Tuple[float, float], Tuple[float, float]
        ] = ((-1000.0, 1000.0), (-1000.0, 1000.0), (-1000.0, 1000.0)),
        rotation_limits: Tuple[
            Tuple[float, float], Tuple[float, float], Tuple[float, float]
        ] = ((-1000.0, 1000.0), (-1000.0, 1000.0), (-1000.0, 1000.0)),
        depth_test: bool = True,
        opacity: float = 1.0,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> TransformControlsHandle:
        """Add a transform gizmo for interacting with the scene.

        This method adds a transform control (gizmo) to the scene, allowing for interactive
        manipulation of objects in terms of their position, rotation, and scale.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            scale: Scale of the transform controls.
            line_width: Width of the lines used in the gizmo.
            fixed: Boolean indicating if the gizmo should be fixed in position.
            auto_transform: Whether the transform should be applied automatically.
            active_axes: Tuple of booleans indicating active axes.
            disable_axes: Boolean to disable axes interaction.
            disable_sliders: Boolean to disable slider interaction.
            disable_rotations: Boolean to disable rotation interaction.
            translation_limits: Limits for translation.
            rotation_limits: Limits for rotation.
            depth_test: Boolean indicating if depth testing should be used when rendering.
            opacity: Opacity of the gizmo.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation from parent frame to local frame (t_pl).
            visible: Whether or not this gizmo is initially visible.

        Returns:
            Handle for manipulating (and reading state of) scene node.
        """
        self._queue(
            _messages.TransformControlsMessage(
                name=name,
                scale=scale,
                line_width=line_width,
                fixed=fixed,
                auto_transform=auto_transform,
                active_axes=active_axes,
                disable_axes=disable_axes,
                disable_sliders=disable_sliders,
                disable_rotations=disable_rotations,
                translation_limits=translation_limits,
                rotation_limits=rotation_limits,
                depth_test=depth_test,
                opacity=opacity,
            )
        )

        def sync_cb(client_id: ClientId, state: TransformControlsHandle) -> None:
            message_orientation = _messages.SetOrientationMessage(
                name=name,
                wxyz=tuple(map(float, state._impl.wxyz)),  # type: ignore
            )
            message_orientation.excluded_self_client = client_id
            self._queue(message_orientation)

            message_position = _messages.SetPositionMessage(
                name=name,
                position=tuple(map(float, state._impl.position)),  # type: ignore
            )
            message_position.excluded_self_client = client_id
            self._queue(message_position)

        node_handle = SceneNodeHandle._make(self, name, wxyz, position, visible)
        state_aux = _TransformControlsState(
            last_updated=time.time(),
            update_cb=[],
            sync_cb=sync_cb,
        )
        handle = TransformControlsHandle(node_handle._impl, state_aux)
        self._handle_from_transform_controls_name[name] = handle
        return handle

    def reset_scene(self) -> None:
        """Reset the scene."""
        self._queue(_messages.ResetSceneMessage())

    def _queue(self, message: _messages.Message) -> None:
        """Wrapped method for sending messages safely."""
        got_lock = self._atomic_lock.acquire(blocking=False)
        if got_lock:
            self._queue_unsafe(message)
            self._atomic_lock.release()
        else:
            # Send when lock is acquirable, while retaining message order.
            # This could be optimized!
            self._queued_messages.put(message)

            def try_again() -> None:
                with self._atomic_lock:
                    self._queue_unsafe(self._queued_messages.get())

            threading.Thread(target=try_again).start()

    @abc.abstractmethod
    def _queue_unsafe(self, message: _messages.Message) -> None:
        """Abstract method for sending messages."""
        ...

    def _get_client_handle(self, client_id: ClientId) -> ClientHandle:
        """Private helper for getting a client handle from its ID."""
        # Avoid circular imports.
        from ._viser import ClientHandle, ViserServer

        # Implementation-wise, note that MessageApi is never directly instantiated.
        # Instead, it serves as a mixin/base class for either ViserServer, which
        # maintains a registry of connected clients, or ClientHandle, which should
        # only ever be dealing with its own client_id.
        if isinstance(self, ViserServer):
            # TODO: there's a potential race condition here when the client disconnects.
            # This probably applies to multiple other parts of the code, we should
            # revisit all of the cases where we index into connected_clients.
            return self._state.connected_clients[client_id]
        else:
            assert isinstance(self, ClientHandle)
            assert client_id == self.client_id
            return self

    def _handle_transform_controls_updates(
        self, client_id: ClientId, message: _messages.TransformControlsUpdateMessage
    ) -> None:
        """Callback for handling transform gizmo messages."""
        handle = self._handle_from_transform_controls_name.get(message.name, None)
        if handle is None:
            return

        # Update state.
        with self.atomic():
            handle._impl.wxyz = onp.array(message.wxyz)
            handle._impl.position = onp.array(message.position)
            handle._impl_aux.last_updated = time.time()

        # Trigger callbacks.
        for cb in handle._impl_aux.update_cb:
            cb(handle)
        if handle._impl_aux.sync_cb is not None:
            handle._impl_aux.sync_cb(client_id, handle)

    def _handle_node_click_updates(
        self, client_id: ClientId, message: _messages.SceneNodeClickMessage
    ) -> None:
        """Callback for handling click messages."""
        handle = self._handle_from_node_name.get(message.name, None)
        if handle is None or handle._impl.click_cb is None:
            return
        for cb in handle._impl.click_cb:
            event = SceneNodePointerEvent(
                client=self._get_client_handle(client_id),
                client_id=client_id,
                event="click",
                target=handle,
                ray_origin=message.ray_origin,
                ray_direction=message.ray_direction,
            )
            cb(event)  # type: ignore

    def _handle_scene_pointer_updates(
        self, client_id: ClientId, message: _messages.ScenePointerMessage
    ):
        """Callback for handling click messages."""
        for cb in self._scene_pointer_cb:
            event = ScenePointerEvent(
                client=self._get_client_handle(client_id),
                client_id=client_id,
                event=message.event_type,
                ray_origin=message.ray_origin,
                ray_direction=message.ray_direction,
            )
            cb(event)

    def on_scene_click(
        self,
        func: Callable[[ScenePointerEvent], None],
    ) -> Callable[[ScenePointerEvent], None]:
        """Add a callback for scene pointer events.

        Args:
            func: The callback function to add.
        """
        self._scene_pointer_cb.append(func)

        # If this is the first callback.
        if len(self._scene_pointer_cb) == 1:
            self._queue(_messages.SceneClickEnableMessage(enable=True))
        return func

    def remove_scene_click_callback(
        self,
        func: Callable[[ScenePointerEvent], None],
    ) -> None:
        """Check for the function handle in the list of callbacks and remove it.

        Args:
            func: The callback function to remove.
        """
        if func in self._scene_pointer_cb:
            self._scene_pointer_cb.remove(func)

        # Notify client that the listener has been removed.
        if len(self._scene_pointer_cb) == 0:
            from ._viser import ViserServer

            if isinstance(self, ViserServer):
                # Turn off server-level scene click events.
                self._queue(_messages.SceneClickEnableMessage(enable=False))

                # Catch an unlikely edge case: we need to re-enable click events for
                # clients that still have callbacks.
                clients = self.get_clients()
                if len(clients) > 0:
                    # TODO: putting this in a thread with an initial sleep is a hack for
                    # giving us a soft guarantee on message ordering; the enable messages
                    # need to arrive after the disable one above.
                    #
                    # Ideally we should implement a flush() method of some kind that
                    # empties the message buffer.

                    def reenable() -> None:
                        time.sleep(1.0 / 60.0)
                        for client in clients.values():
                            if len(client._scene_pointer_cb) > 0:
                                self._queue(
                                    _messages.SceneClickEnableMessage(enable=True)
                                )

                    threading.Thread(target=reenable).start()

            else:
                assert isinstance(self, ClientHandle)

                # Turn off scene click events for clients, but only if there's no
                # server-level scene click events.
                if len(self._state.viser_server._scene_pointer_cb) == 0:
                    self._queue(_messages.SceneClickEnableMessage(enable=False))

    def add_3d_gui_container(
        self,
        name: str,
        wxyz: Tuple[float, float, float, float] | onp.ndarray = (1.0, 0.0, 0.0, 0.0),
        position: Tuple[float, float, float] | onp.ndarray = (0.0, 0.0, 0.0),
        visible: bool = True,
    ) -> Gui3dContainerHandle:
        """Add a 3D gui container to the scene. The returned container handle can be
        used as a context to place GUI elements into the 3D scene.

        Args:
            name: A scene tree name. Names in the format of /parent/child can be used to
                define a kinematic tree.
            wxyz: Quaternion rotation to parent frame from local frame (R_pl).
            position: Translation to parent frame from local frame (t_pl).
            visible: Whether or not this scene node is initially visible.

        Returns:
            Handle for manipulating scene node. Can be used as a context to place GUI
            elements inside of the container.
        """

        # Avoids circular import.
        from ._gui_api import GuiApi, _make_unique_id

        # New name to make the type checker happy; ViserServer and ClientHandle inherit
        # from both GuiApi and MessageApi. The pattern below is unideal.
        gui_api = self
        assert isinstance(gui_api, GuiApi)

        # Remove the 3D GUI container if it already exists. This will make sure
        # contained GUI elements are removed, preventing potential memory leaks.
        if name in gui_api._handle_from_node_name:
            gui_api._handle_from_node_name[name].remove()

        container_id = _make_unique_id()
        self._queue(
            _messages.Gui3DMessage(
                order=time.time(),
                name=name,
                container_id=container_id,
            )
        )
        node_handle = SceneNodeHandle._make(self, name, wxyz, position, visible=visible)
        return Gui3dContainerHandle(node_handle._impl, gui_api, container_id)

    def send_file_download(
        self, filename: str, content: bytes, chunk_size: int = 1024 * 1024
    ) -> None:
        """Send a file for a client or clients to download.

        Args:
            filename: Name of the file to send. Used to infer MIME type.
            content: Content of the file.
            chunk_size: Number of bytes to send at a time.
        """
        mime_type = mimetypes.guess_type(filename, strict=False)[0]
        assert (
            mime_type is not None
        ), f"Could not guess MIME type from filename {filename}!"

        from ._gui_api import _make_unique_id

        parts = [
            content[i * chunk_size : (i + 1) * chunk_size]
            for i in range(int(onp.ceil(len(content) / chunk_size)))
        ]

        uuid = _make_unique_id()
        self._queue(
            _messages.FileDownloadStart(
                download_uuid=uuid,
                filename=filename,
                mime_type=mime_type,
                part_count=len(parts),
                size_bytes=len(content),
            )
        )

        for i, part in enumerate(parts):
            self._queue(_messages.FileDownloadPart(uuid, part=i, content=part))
            self.flush()

    @abc.abstractmethod
    def flush(self) -> None:
        """Flush the outgoing message buffer. Any buffered messages will immediately be
        sent. (by default they are windowed)"""
        raise NotImplementedError()

    @contextlib.contextmanager
    @abc.abstractmethod
    def atomic(self) -> Generator[None, None, None]:
        """Returns a context where: all outgoing messages are grouped and applied by
        clients atomically.

        This can be helpful for things like animations, or when we want position and
        orientation updates to happen synchronously.
        """
        raise NotImplementedError()
