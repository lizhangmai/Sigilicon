"""Immutable, deterministic Spectre subcircuit source model."""

from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Sequence


@dataclass(frozen=True)
class NetlistSnapshot:
    """One byte-for-byte source read and the model parsed from those bytes."""

    source_path: Path
    text: str
    interfaces: Mapping[str, tuple[str, ...]]

    @property
    def subckts(self) -> tuple[str, ...]:
        return tuple(self.interfaces)


@dataclass(frozen=True)
class NetlistInstance:
    """One canonical Spectre instance statement inside a subcircuit."""

    name: str
    nodes: tuple[str, ...]
    master: str
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class NetlistSubcircuit:
    """One parsed subcircuit definition and its immutable source provenance."""

    name: str
    ports: tuple[str, ...]
    parameters: tuple[str, ...]
    statements: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True)
class NetlistHierarchy:
    """Resolved, recursively counted hierarchy rooted at one canonical subcircuit."""

    top: str
    definitions: Mapping[str, NetlistSubcircuit]
    direct_children: Mapping[str, Mapping[str, int]]
    reachable_counts: Mapping[str, int]
    primitive_counts: Mapping[str, int]
    unreachable_subckts: tuple[str, ...]
    dependency_order: tuple[str, ...]


@dataclass(frozen=True)
class SpectrePWLSource:
    """One inline Spectre PWL voltage source and its ordered time/value table."""

    instance: str
    points: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MaterializedNetlist:
    """A nofollow artifact bound to one read-only regular-file inode."""

    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    parent_device: int
    parent_inode: int

    @contextmanager
    def open_fd(self) -> Iterator[int]:
        """Own the attested artifact fd until its external consumer exits."""

        parent_fd = _open_nofollow_directory(
            self.path.parent,
            create_missing=False,
        )
        descriptor: int | None = None
        try:
            parent_metadata = os.fstat(parent_fd)
            if (parent_metadata.st_dev, parent_metadata.st_ino) != (
                self.parent_device,
                self.parent_inode,
            ):
                raise RuntimeError(
                    f"immutable netlist artifact parent identity changed: "
                    f"{self.path.parent}"
                )
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            metadata = os.fstat(descriptor)
            identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            if identity != (self.device, self.inode, self.size, self.mtime_ns):
                raise RuntimeError(
                    f"immutable netlist artifact identity changed: {self.path}"
                )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o222
            ):
                raise RuntimeError(
                    "immutable netlist artifact is not a read-only, single-link "
                    f"regular file: {self.path}"
                )
            visible = os.stat(
                self.path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (visible.st_dev, visible.st_ino) != (self.device, self.inode):
                raise RuntimeError(
                    f"immutable netlist artifact pathname was replaced: {self.path}"
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def verify(self) -> Path:
        """Re-attest the artifact without transferring fd ownership."""

        with self.open_fd():
            pass
        return self.path


def _open_nofollow_directory(path: Path, *, create_missing: bool = True) -> int:
    """Open/create an absolute directory chain without following any symlink."""

    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _code(raw_line: str) -> str:
    return raw_line.split("//", 1)[0].strip()


def iter_spectre_logical_lines(text: str) -> Iterator[str]:
    """Yield non-empty Spectre statements with comments and continuations resolved.

    This is deliberately syntax-level only: callers retain responsibility for
    interpreting subcircuits, devices, or simulator-specific semantics.
    """

    pending = ""
    for raw_line in text.splitlines():
        line = _code(raw_line)
        if not line:
            continue
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        yield line
    if pending:
        raise ValueError("unterminated Spectre continuation")


def _parse_subckt_interfaces(text: str, *, source: Path) -> dict[str, tuple[str, ...]]:
    interfaces: dict[str, tuple[str, ...]] = {}
    for line in iter_spectre_logical_lines(text):
        if not line.lower().startswith("subckt "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name in interfaces:
            raise ValueError(f"duplicate subckt: {name}")
        port_tokens = parts[2:]
        parameter_index = next(
            (i for i, token in enumerate(port_tokens) if token.lower() == "parameters"),
            len(port_tokens),
        )
        interfaces[name] = tuple(port_tokens[:parameter_index])
    if not interfaces:
        raise ValueError(f"no subckt line found in {source}")
    return interfaces


def load_netlist_snapshot(netlist: Path) -> NetlistSnapshot:
    """Read and parse a canonical netlist exactly once.

    Invalid UTF-8 is rejected rather than replaced because the parsed model and
    bytes handed to Cadence must describe one identical source read.
    """

    source = Path(os.path.abspath(netlist))
    parent_fd = _open_nofollow_directory(source.parent, create_missing=False)
    try:
        descriptor = os.open(
            source.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"netlist source is not a regular file: {source}")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            visible = os.stat(
                source.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or (visible.st_dev, visible.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError(f"netlist source changed while it was read: {source}")
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    text = payload.decode("utf-8")
    interfaces = _parse_subckt_interfaces(text, source=source)
    return NetlistSnapshot(
        source_path=source,
        text=text,
        interfaces=MappingProxyType(interfaces),
    )


def materialize_netlist_snapshot(
    snapshot: NetlistSnapshot,
    destination: Path,
) -> MaterializedNetlist:
    """Create or verify a read-only artifact containing the exact snapshot bytes."""

    payload = snapshot.text.encode("utf-8")
    destination = Path(os.path.abspath(destination))
    parent_fd = _open_nofollow_directory(destination.parent)
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                destination.name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o444,
                dir_fd=parent_fd,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                destination.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        if created:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise RuntimeError(
                        f"could not write immutable netlist artifact: {destination}"
                    )
                remaining = remaining[written:]
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            os.fsync(parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(
                f"immutable netlist artifact is not a single-link regular file: "
                f"{destination}"
            )
        if metadata.st_mode & 0o222:
            raise RuntimeError(f"immutable netlist artifact is writable: {destination}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        if b"".join(chunks) != payload:
            raise RuntimeError(
                f"immutable netlist artifact has conflicting content: {destination}"
            )
        visible = os.stat(
            destination.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(
                f"immutable netlist artifact pathname changed: {destination}"
            )
        artifact = MaterializedNetlist(
            path=destination,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            parent_device=os.fstat(parent_fd).st_dev,
            parent_inode=os.fstat(parent_fd).st_ino,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    artifact.verify()
    return artifact


def discover_subckt_interfaces(
    netlist: Path | NetlistSnapshot,
) -> dict[str, tuple[str, ...]]:
    """Return ``subckt -> ordered ports`` in source order."""

    snapshot = (
        netlist if isinstance(netlist, NetlistSnapshot) else load_netlist_snapshot(netlist)
    )
    return dict(snapshot.interfaces)


def discover_subckts(netlist: Path | NetlistSnapshot) -> tuple[str, ...]:
    if isinstance(netlist, NetlistSnapshot):
        return netlist.subckts
    return load_netlist_snapshot(netlist).subckts


def subckt_ports(netlist: Path | NetlistSnapshot, cell: str) -> tuple[str, ...]:
    interfaces = discover_subckt_interfaces(netlist)
    try:
        return interfaces[cell]
    except KeyError as exc:
        raise ValueError(f"subckt not found: {cell}") from exc


def extract_subckt_body(netlist: Path | NetlistSnapshot, cell: str) -> str:
    """Return source text between ``subckt <cell>`` and its ``ends``."""

    snapshot = (
        netlist if isinstance(netlist, NetlistSnapshot) else load_netlist_snapshot(netlist)
    )
    lines = snapshot.text.splitlines()
    start: int | None = None
    header_continues = False
    body: list[str] = []
    for index, raw_line in enumerate(lines):
        parts = _code(raw_line).split()
        if start is None:
            if len(parts) >= 2 and parts[0].lower() == "subckt" and parts[1] == cell:
                start = index
                header_continues = _code(raw_line).rstrip().endswith("\\")
            continue
        if header_continues:
            code = _code(raw_line).rstrip()
            if code:
                header_continues = code.endswith("\\")
            continue
        if parts and parts[0].lower() == "ends":
            if len(parts) == 1 or parts[1] == cell:
                while body and not body[-1].strip():
                    body.pop()
                if not body:
                    raise ValueError(f"subckt body is empty: {cell}")
                return "\n".join(body)
        body.append(raw_line)
    if start is None:
        raise ValueError(f"subckt not found: {cell}")
    raise ValueError(f"unterminated subckt: {cell}")


def parse_spectre_pwl_sources(
    netlist: Path | NetlistSnapshot,
    cell: str,
    *,
    max_points: int = 50,
) -> tuple[SpectrePWLSource, ...]:
    """Parse inline ``vsource type=pwl wave=[...]`` tables for OA import.

    The analogLib voltage-source CDF in supported Cadence releases exposes at
    most 50 explicit voltage/time pairs.  Rejecting a larger table here avoids
    silently truncating canonical stimulus during source-to-OA materialization.
    """

    if max_points <= 0:
        raise ValueError("max_points must be positive")
    body = extract_subckt_body(netlist, cell)
    result: list[SpectrePWLSource] = []
    names: set[str] = set()
    for line in iter_spectre_logical_lines(body):
        instance = _SPECTRE_INSTANCE.fullmatch(line)
        if instance is None or instance.group("master").lower() != "vsource":
            continue
        parameters = instance.group("parameters") or ""
        if re.search(r"(?:^|\s)type\s*=\s*pwl(?:\s|$)", parameters, re.I) is None:
            continue
        wave = re.search(r"(?:^|\s)wave\s*=\s*\[([^\]]*)\]", parameters, re.I)
        if wave is None:
            raise ValueError(
                f"inline PWL source lacks wave table: {cell}/{instance.group('name')}"
            )
        values = tuple(wave.group(1).split())
        if not values or len(values) % 2:
            raise ValueError(
                f"inline PWL source must contain time/value pairs: "
                f"{cell}/{instance.group('name')}"
            )
        points = tuple(zip(values[0::2], values[1::2], strict=True))
        if len(points) > max_points:
            raise ValueError(
                f"inline PWL source exceeds analogLib's {max_points}-pair table: "
                f"{cell}/{instance.group('name')} has {len(points)} pairs"
            )
        name = instance.group("name")
        if name in names:
            raise ValueError(f"duplicate inline PWL source instance: {cell}/{name}")
        names.add(name)
        result.append(SpectrePWLSource(instance=name, points=points))
    return tuple(result)


def select_subckt_snapshot(snapshot: NetlistSnapshot, cell: str) -> NetlistSnapshot:
    """Return the exact declared subckt as an immutable one-cell snapshot.

    This supports OA cells that share a human-maintained source file but are
    synchronized independently. Selection is a framework operation so design
    runners cannot invent private source slices.
    """

    ports = subckt_ports(snapshot, cell)
    lines = snapshot.text.splitlines()
    start: int | None = None
    stop: int | None = None
    for index, raw_line in enumerate(lines):
        tokens = _code(raw_line).split()
        if start is None:
            if len(tokens) >= 2 and tokens[0].lower() == "subckt" and tokens[1] == cell:
                start = index
            continue
        if tokens and tokens[0].lower() == "ends" and (
            len(tokens) == 1 or tokens[1] == cell
        ):
            stop = index + 1
            break
    if start is None:
        raise ValueError(f"subckt not found: {cell}")
    if stop is None:
        raise ValueError(f"unterminated subckt: {cell}")
    text = "\n".join(lines[start:stop]) + "\n"
    interfaces = _parse_subckt_interfaces(text, source=snapshot.source_path)
    if tuple(interfaces) != (cell,) or interfaces[cell] != ports:
        raise RuntimeError("selected subckt snapshot changed its declared interface")
    return NetlistSnapshot(
        source_path=snapshot.source_path,
        text=text,
        interfaces=MappingProxyType(interfaces),
    )


_SPECTRE_INSTANCE = re.compile(
    r"^(?P<name>[^\s()]+)\s+\((?P<nodes>[^()]*)\)\s+"
    r"(?P<master>[^\s()]+)(?:\s+(?P<parameters>.*))?$"
)


def parse_subcircuit_definitions(
    snapshots: Sequence[NetlistSnapshot],
) -> Mapping[str, NetlistSubcircuit]:
    """Parse all definitions from exact source snapshots.

    Duplicate cell names are rejected even when their text happens to match.
    Requiring one authoritative definition prevents a dependency search path
    from silently selecting a different schematic contract.
    """

    definitions: dict[str, NetlistSubcircuit] = {}
    for snapshot in snapshots:
        active_name: str | None = None
        active_ports: tuple[str, ...] = ()
        active_parameters: tuple[str, ...] = ()
        statements: list[str] = []
        for line in iter_spectre_logical_lines(snapshot.text):
            tokens = line.split()
            keyword = tokens[0].lower()
            if active_name is None:
                if keyword != "subckt":
                    continue
                if len(tokens) < 2:
                    raise ValueError(
                        f"malformed subckt header in {snapshot.source_path}: {line}"
                    )
                active_name = tokens[1]
                remainder = tokens[2:]
                parameter_index = next(
                    (
                        index
                        for index, token in enumerate(remainder)
                        if token.lower() == "parameters"
                    ),
                    len(remainder),
                )
                active_ports = tuple(remainder[:parameter_index])
                active_parameters = tuple(remainder[parameter_index + 1 :])
                statements = []
                continue
            if keyword == "subckt":
                raise ValueError(
                    f"nested subckt {tokens[1]} inside {active_name} in "
                    f"{snapshot.source_path}"
                )
            if keyword != "ends":
                statements.append(line)
                continue
            if len(tokens) > 1 and tokens[1] != active_name:
                raise ValueError(
                    f"ends {tokens[1]} does not match subckt {active_name} in "
                    f"{snapshot.source_path}"
                )
            if active_name in definitions:
                first = definitions[active_name].source_path
                raise ValueError(
                    f"duplicate subckt definition {active_name}: {first} and "
                    f"{snapshot.source_path}"
                )
            definitions[active_name] = NetlistSubcircuit(
                name=active_name,
                ports=active_ports,
                parameters=active_parameters,
                statements=tuple(statements),
                source_path=snapshot.source_path,
            )
            active_name = None
            active_ports = ()
            active_parameters = ()
            statements = []
        if active_name is not None:
            raise ValueError(
                f"unterminated subckt {active_name} in {snapshot.source_path}"
            )
    if not definitions:
        raise ValueError("no subcircuit definitions were supplied")
    return MappingProxyType(definitions)


def parse_subcircuit_instances(
    definition: NetlistSubcircuit,
) -> tuple[NetlistInstance, ...]:
    """Parse every device/subcircuit statement without interpreting parameters."""

    instances: list[NetlistInstance] = []
    names: set[str] = set()
    for statement in definition.statements:
        if statement.lower().startswith("parameters "):
            continue
        match = _SPECTRE_INSTANCE.fullmatch(statement)
        if match is None:
            raise ValueError(
                f"unsupported statement in subckt {definition.name}: {statement}"
            )
        name = match.group("name")
        if name in names:
            raise ValueError(f"duplicate instance {name} in subckt {definition.name}")
        names.add(name)
        parameter_text = match.group("parameters") or ""
        instances.append(
            NetlistInstance(
                name=name,
                nodes=tuple(match.group("nodes").split()),
                master=match.group("master"),
                parameters=tuple(parameter_text.split()),
            )
        )
    return tuple(instances)


def resolve_netlist_hierarchy(
    snapshots: Sequence[NetlistSnapshot],
    *,
    top: str,
    primitive_masters: Sequence[str],
) -> NetlistHierarchy:
    """Resolve, validate, and count one canonical hierarchy.

    Only explicitly supplied primitive masters may remain undefined.  Every
    project subcircuit instance must resolve to exactly one definition, and its
    node count must match the child interface.  This makes missing masters and
    accidental blackboxes hard errors rather than LVS-time surprises.
    """

    definitions = parse_subcircuit_definitions(snapshots)
    if top not in definitions:
        raise ValueError(f"top subckt not found: {top}")
    primitives = frozenset(primitive_masters)
    if primitives & definitions.keys():
        overlap = ", ".join(sorted(primitives & definitions.keys()))
        raise ValueError(f"primitive masters also have subckt definitions: {overlap}")

    instances_by_cell = {
        name: parse_subcircuit_instances(definition)
        for name, definition in definitions.items()
    }
    direct_children: dict[str, Mapping[str, int]] = {}
    for name, instances in instances_by_cell.items():
        children: Counter[str] = Counter()
        for instance in instances:
            if instance.master in definitions:
                children[instance.master] += 1
        direct_children[name] = MappingProxyType(dict(sorted(children.items())))

    memo: dict[str, tuple[Counter[str], Counter[str], tuple[str, ...]]] = {}
    active: list[str] = []

    def expand(cell: str) -> tuple[Counter[str], Counter[str], tuple[str, ...]]:
        if cell in memo:
            return memo[cell]
        if cell in active:
            cycle = " -> ".join((*active[active.index(cell) :], cell))
            raise ValueError(f"recursive subckt hierarchy: {cycle}")
        active.append(cell)
        cells: Counter[str] = Counter({cell: 1})
        primitive_counts: Counter[str] = Counter()
        order: list[str] = []
        for instance in instances_by_cell[cell]:
            if instance.master in definitions:
                expected = len(definitions[instance.master].ports)
                if len(instance.nodes) != expected:
                    raise ValueError(
                        f"instance {cell}/{instance.name} of {instance.master} has "
                        f"{len(instance.nodes)} nodes; expected {expected}"
                    )
                child_cells, child_primitives, child_order = expand(instance.master)
                cells.update(child_cells)
                primitive_counts.update(child_primitives)
                for child in child_order:
                    if child not in order:
                        order.append(child)
            elif instance.master in primitives:
                primitive_counts[instance.master] += 1
            else:
                raise ValueError(
                    f"missing master {instance.master} for instance "
                    f"{cell}/{instance.name}"
                )
        if cell not in order:
            order.append(cell)
        active.pop()
        result = cells, primitive_counts, tuple(order)
        memo[cell] = result
        return result

    reachable, primitive_counts, dependency_order = expand(top)
    reachable_names = set(reachable)
    return NetlistHierarchy(
        top=top,
        definitions=definitions,
        direct_children=MappingProxyType(direct_children),
        reachable_counts=MappingProxyType(dict(sorted(reachable.items()))),
        primitive_counts=MappingProxyType(dict(sorted(primitive_counts.items()))),
        unreachable_subckts=tuple(
            name for name in definitions if name not in reachable_names
        ),
        dependency_order=dependency_order,
    )


def render_canonical_cdl(
    hierarchy: NetlistHierarchy,
    *,
    primitive_subcircuit_masters: Sequence[str] = (),
) -> str:
    """Mechanically render the resolved canonical Spectre hierarchy as CDL.

    Spectre instance names are not device designators, while CDL uses the first
    character to select the statement type.  PDK primitives implemented as
    subcircuits therefore need an ``X`` designator even when their canonical
    Spectre instance name happens to start with (for example) ``R``.
    """

    subcircuit_primitives = frozenset(primitive_subcircuit_masters)
    lines: list[str] = []
    for name in hierarchy.dependency_order:
        definition = hierarchy.definitions[name]
        local_parameters = list(definition.parameters)
        for statement in definition.statements:
            if statement.lower().startswith("parameters "):
                local_parameters.extend(statement.split()[1:])
        header = f".SUBCKT {name} {' '.join(definition.ports)}".rstrip()
        if local_parameters:
            header += " PARAMS: " + " ".join(local_parameters)
        lines.append(header)
        for statement in definition.statements:
            if statement.lower().startswith("parameters "):
                continue
            instance = parse_subcircuit_instances(
                NetlistSubcircuit(
                    name=name,
                    ports=definition.ports,
                    parameters=definition.parameters,
                    statements=(statement,),
                    source_path=definition.source_path,
                )
            )[0]
            suffix = (
                " " + " ".join(instance.parameters) if instance.parameters else ""
            )
            cdl_name = instance.name
            if instance.master in subcircuit_primitives and not cdl_name.upper().startswith("X"):
                cdl_name = f"X{cdl_name}"
            lines.append(
                f"{cdl_name} {' '.join(instance.nodes)} {instance.master}{suffix}"
            )
        lines.append(f".ENDS {name}")
        lines.append("")
    return "\n".join(lines)


_PARAMETER_DEFAULT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)=(?P<value>[^\s]+)\Z"
)
_LITERAL_NUMERIC_DEFAULT = re.compile(
    r"[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?"
    r"(?:meg|[afpnumkgt])?\Z",
    re.IGNORECASE,
)
_PARAMETER_REFERENCE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_$])[A-Za-z_][A-Za-z0-9_$]*\s*=\s*)"
    r"(?P<value>[A-Za-z_][A-Za-z0-9_$]*)(?=\s|\Z)"
)


def lower_subckt_default_parameters(
    netlist: Path | NetlistSnapshot,
    cell: str,
) -> NetlistSnapshot:
    """Mechanically elaborate a selected subckt's literal defaults.

    Some OA importers accept a Spectre ``subckt parameters`` header but reject
    MOS assignments such as ``l=lch``.  This generic lowering is intentionally
    narrow: it only substitutes exact identifier RHS values with literal
    defaults declared by that same subckt, writes one selected subckt, and
    otherwise refuses unsupported syntax rather than guessing.  The caller
    retains both this derived input and the canonical source as provenance.
    """

    snapshot = (
        netlist if isinstance(netlist, NetlistSnapshot) else load_netlist_snapshot(netlist)
    )
    ports = subckt_ports(snapshot, cell)
    header: str | None = None
    header_was_continued = False
    for raw_line in snapshot.text.splitlines():
        tokens = _code(raw_line).split()
        if len(tokens) >= 2 and tokens[0].lower() == "subckt" and tokens[1] == cell:
            header_was_continued = raw_line.rstrip().endswith("\\")
            break
    for logical_line in iter_spectre_logical_lines(snapshot.text):
        tokens = logical_line.split()
        if len(tokens) >= 2 and tokens[0].lower() == "subckt" and tokens[1] == cell:
            header = logical_line
            break
    if header is None:
        raise ValueError(f"subckt header not found: {cell}")
    tokens = header.split()
    remainder = tokens[2:]
    body_source = extract_subckt_body(snapshot, cell)
    logical_body = tuple(iter_spectre_logical_lines(body_source))

    def materialize_spicein(body_lines: Iterable[str]) -> NetlistSnapshot:
        lowered_text = "\n".join(
            (
                f"subckt {cell} {' '.join(ports)}",
                *body_lines,
                f"ends {cell}",
                "",
            )
        )
        interfaces = _parse_subckt_interfaces(
            lowered_text, source=snapshot.source_path
        )
        if tuple(interfaces) != (cell,) or interfaces[cell] != ports:
            raise RuntimeError(
                "normalized spiceIn source changed the declared subckt interface"
            )
        return NetlistSnapshot(
            source_path=snapshot.source_path,
            text=lowered_text,
            interfaces=MappingProxyType(interfaces),
        )

    body_parameter_line: str | None = None
    try:
        parameter_index = remainder.index("parameters")
        default_tokens = remainder[parameter_index + 1 :]
    except ValueError:
        # Spectre's directly executable form declares subcircuit defaults on a
        # separate first body statement. Support it generically while retaining
        # the older importer-oriented header form for existing canonical input.
        if not logical_body or not logical_body[0].lower().startswith("parameters "):
            if not header_was_continued and "\\" not in body_source:
                return snapshot
            return materialize_spicein(logical_body)
        body_parameter_line = logical_body[0]
        default_tokens = body_parameter_line.split()[1:]
    defaults: dict[str, str] = {}
    for token in default_tokens:
        match = _PARAMETER_DEFAULT.fullmatch(token)
        if match is None:
            raise ValueError(
                "cannot lower non-literal subckt default parameter for spiceIn: "
                + token
            )
        if _LITERAL_NUMERIC_DEFAULT.fullmatch(match.group("value")) is None:
            raise ValueError(
                "cannot lower non-literal subckt default parameter for spiceIn: "
                + token
            )
        defaults[match.group("name")] = match.group("value")
    if not defaults:
        raise ValueError(f"subckt {cell} declares parameters without defaults")

    def lower_line(raw_line: str) -> str:
        code, separator, comment = raw_line.partition("//")

        def replace(match: re.Match[str]) -> str:
            value = match.group("value")
            return match.group("prefix") + defaults.get(value, value)

        return _PARAMETER_REFERENCE.sub(replace, code) + separator + comment

    body_lines = list(logical_body)
    if body_parameter_line is not None:
        if not body_lines or body_lines[0] != body_parameter_line:
            raise ValueError("cannot locate subckt parameters statement for spiceIn")
        body_lines.pop(0)
    return materialize_spicein(lower_line(line) for line in body_lines)


def order_subckts(subckts: tuple[str, ...], *, top: str | None = None) -> tuple[str, ...]:
    """Return source-order subckts with the selected top last.

    This is intentionally not a dependency sorter; canonical sources must place
    child definitions before parents until a full Spectre parser is introduced.
    """

    if not subckts:
        raise ValueError("subckt list must not be empty")
    selected_top = top or subckts[-1]
    if selected_top not in subckts:
        raise ValueError(f"top subckt not found: {selected_top}")
    return tuple(name for name in subckts if name != selected_top) + (selected_top,)
