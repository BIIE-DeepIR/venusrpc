"""Runtime labware placement — thin wrappers over the Venus ``device`` methods.

Unlike the deck layout (``.lay``), which fixes where labware sits at design
time, these call the built-in HSL ``device`` methods that add / move / remove
labware *while a method is running*:

* :meth:`Labware.add_labware` — place a labware at an absolute deck pose.
* :meth:`Labware.add_labware_to_template` — place a labware onto a carrier
  template site (i.e. "put a plate on a rack/carrier slot").
* :meth:`Labware.add_container_to_rack` — place a container into a rack site.
* :meth:`Labware.remove_labware` — remove a placed labware.
* :meth:`Labware.get_labware_position` — read a labware's current X/Y/Z/angle.
* :meth:`Labware.get_template_labware_names` — enumerate carrier templates and
  the labware currently on them.

These are *built-in* device methods (the host script already calls
``__DEVICE__.GetLabwarePosition(...)`` directly), so no extra ``#include`` is
needed — only ``VirtualLabware_V2``'s higher-level helpers would require one.

Marshalling reuses :class:`~venusrpc.types.Array` (for the ``position[4]`` /
``offset[3]`` arrays) and :class:`~venusrpc.types.Variable` (to capture the
status int each method returns; ``1`` means success).
"""

from __future__ import annotations

from venusrpc import _hsl
from venusrpc.connection import Connection
from venusrpc.types import Array, Device, Variable


class LabwarePlacementError(RuntimeError):
    """A device labware method returned a non-success status."""

    def __init__(self, method: str, status: int, labware_id: str) -> None:
        super().__init__(
            f"{method} returned {status} (expected 1) for labware {labware_id!r}"
        )
        self.method = method
        self.status = status
        self.labware_id = labware_id


class Labware:
    """Runtime labware placement against a Venus ``device``.

    One instance per :class:`~venusrpc.connection.Connection`. Every method
    takes the :class:`~venusrpc.types.Device` whose deck is being mutated.
    """

    __slots__ = ("_con",)

    def __init__(self, con: Connection) -> None:
        self._con = con

    # -- internal -----------------------------------------------------------

    def _call_status(self, expr: str, *, method: str, labware_id: str,
                     check: bool) -> int:
        """Run ``<res> = <device>.<method>(...);`` and return the status int."""
        res = Variable(self._con, 0)
        self._con.execute(f"{res.name} = {expr};")
        status = int(res.pull())
        if check and status != 1:
            raise LabwarePlacementError(method, status, labware_id)
        return status

    # -- placement ----------------------------------------------------------

    def add_labware(
        self,
        device: Device,
        labware_id: str,
        config_file: str,
        x: float,
        y: float,
        z: float,
        angle: float = 0.0,
        *,
        check: bool = True,
    ) -> int:
        """Place ``config_file`` as ``labware_id`` at absolute deck pose.

        ``config_file`` is a Venus labware path (e.g.
        ``r"NGS STAR V\\Ham_96_VB_PCR_200ul.rck"``) or an absolute path.
        ``x``/``y``/``z`` are mm, ``angle`` degrees. Returns the status int
        (``1`` == success); raises :class:`LabwarePlacementError` otherwise
        unless ``check=False``.
        """
        pos = Array(self._con, [float(x), float(y), float(z), float(angle)])
        expr = (
            f"{device.name}.AddLabware("
            f"{_hsl.literal(labware_id)}, {_hsl.literal(config_file)}, {pos.name})"
        )
        return self._call_status(
            expr, method="AddLabware", labware_id=labware_id, check=check
        )

    def add_labware_to_template(
        self,
        device: Device,
        labware_id: str,
        config_file: str,
        template: str,
        site: int | str,
        *,
        check: bool = True,
    ) -> int:
        """Place ``config_file`` as ``labware_id`` onto ``template`` ``site``.

        ``template`` is the carrier's deck Id (e.g. ``"Processing_Carrier"``)
        and ``site`` its 1-based site index. This is the "put a plate on a
        carrier slot" operation. The HSL ``site`` parameter is typed as a
        string, so an int is rendered as its decimal string (``1`` -> ``"1"``).
        """
        expr = (
            f"{device.name}.AddLabwareToTemplate("
            f"{_hsl.literal(labware_id)}, {_hsl.literal(config_file)}, "
            f"{_hsl.literal(template)}, {_hsl.literal(str(site))})"
        )
        return self._call_status(
            expr, method="AddLabwareToTemplate", labware_id=labware_id, check=check
        )

    def add_container_to_rack(
        self,
        device: Device,
        labware_id: str,
        position_id: str,
        container_file: str,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        *,
        check: bool = True,
    ) -> int:
        """Place ``container_file`` into rack ``labware_id`` at ``position_id``.

        ``dx``/``dy``/``dz`` is the offset (mm) applied to the container.
        """
        offset = Array(self._con, [float(dx), float(dy), float(dz)])
        expr = (
            f"{device.name}.AddContainerToRack("
            f"{_hsl.literal(labware_id)}, {_hsl.literal(position_id)}, "
            f"{_hsl.literal(container_file)}, {offset.name})"
        )
        return self._call_status(
            expr, method="AddContainerToRack", labware_id=labware_id, check=check
        )

    def remove_labware(
        self, device: Device, labware_id: str, *, check: bool = False
    ) -> int:
        """Remove placed ``labware_id``. ``check`` defaults to ``False`` since
        removing a non-existent labware is a common, harmless cleanup case."""
        expr = f"{device.name}.RemoveLabware({_hsl.literal(labware_id)})"
        return self._call_status(
            expr, method="RemoveLabware", labware_id=labware_id, check=check
        )

    # -- queries ------------------------------------------------------------

    def get_labware_position(
        self, device: Device, labware_id: str, position_id: str | None = None
    ) -> tuple[float, float, float, float]:
        """Return ``(x, y, z, angle)`` for ``labware_id`` (or a position on it)."""
        out = Array(self._con, [0.0, 0.0, 0.0, 0.0])
        args = [_hsl.literal(labware_id), out.name]
        if position_id is not None:
            args.append(_hsl.literal(position_id))
        self._con.execute(f"{device.name}.GetLabwarePosition({', '.join(args)});")
        vals = out.pull()
        x, y, z, angle = (list(vals) + [0.0, 0.0, 0.0, 0.0])[:4]
        return float(x), float(y), float(z), float(angle)

    def get_template_labware_names(
        self, device: Device
    ) -> list[tuple[str, str]]:
        """Return ``(template_name, labware_id)`` pairs for placed labware."""
        templates = Array(self._con)
        labware = Array(self._con)
        self._con.execute(
            f"{device.name}.GetTemplateLabwareNames({templates.name}, {labware.name});"
        )
        return list(zip(templates.pull(), labware.pull()))
