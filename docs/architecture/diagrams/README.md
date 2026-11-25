# Architecture figures

Solid arrows show data or traffic. Dashed arrows show invocation, control or state checks. Boxes labelled with subnet tiers show placement in the network view. The overview and workflow figures group regional services by responsibility.

| Question | Editable Graphviz source | Portable SVG |
| --- | --- | --- |
| How does a transaction reach a report? | [Overview](sources/overview.dot) | [View](exports/overview.svg) |
| Which network path does each workload use? | [Network](sources/network.dot) | [View](exports/network.svg) |
| Where does a delivery become durable? | [Ingestion](sources/ingestion.dot) | [View](exports/ingestion.svg) |
| What crosses the warehouse boundary? | [Warehouse](sources/warehouse.dot) | [View](exports/warehouse.svg) |
| Who controls internet egress? | [NAT lifecycle](sources/nat.dot) | [View](exports/nat.svg) |

Run `python render.py` from this directory with Graphviz `dot` on PATH. Exports embed the icon bytes so they can travel independently of the source tree.

AWS service and resource icons are from [AWS Icons for PlantUML](https://github.com/awslabs/aws-icons-for-plantuml), copyright Amazon.com, Inc. or its affiliates. Original image files and the [upstream license](icons/LICENSE) accompany the sources.
