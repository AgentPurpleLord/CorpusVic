from dataclasses import dataclass, field

@dataclass
class NodeType:
    id: str
    name: str
    label: str
    description: str | None = None

    # What kind of node is this?
    # Provisions are sections, clauses, etc.
    # Containers are chapters, parts, etc.
    is_provision: bool = False
    is_container: bool = False

    # What content does this node contain?
    allows_text: bool = True
    allows_heading: bool = True

    # What nodes can be children of this node?
    allows_children: list[str] = field(default_factory=list)
    
@dataclass
class Node:
    id: str
    node_type_id: str
    number: str | None = None
    heading: str | None = None
    text: str | None = None
    children: list["Node"] = field(default_factory=list)


class NodeTypeRegistry:
    def __init__(self):
        self._types: dict[str, NodeType] = {}

    def register(self, node_type: NodeType) -> None:
        if node_type.id in self._types:
            raise ValueError(
                f"Node type already exists: {node_type.id}"
            )

        self._types[node_type.id] = node_type

    def get(self, type_id, str) -> NodeType:
        try:
            return self._types[type_id]
        except KeyError:
            raise ValueError(
                f"Unknown node type: {type_id}"
            )

    def exists(self, type_id: str) -> bool:
        return type_id in self._types

    def all(self) -> list[NodeType]:
        return list(self._types.values())