resource "aws_security_group" "ecs" {
  name_prefix = "ecommerce-ecs-"
  description = "Private API tasks"
  vpc_id = aws_vpc.ecommerce.id
}
resource "aws_security_group" "rds" {
  name_prefix = "ecommerce-rds-"
  description = "Private PostgreSQL"
  vpc_id = aws_vpc.ecommerce.id
}
resource "aws_security_group" "worker" {
  name_prefix = "ecommerce-worker-"
  description = "Outbound-only event processing"
  vpc_id = aws_vpc.ecommerce.id
}
resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs.id
  cidr_ipv4 = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port = 443
  to_port = 443
}
resource "aws_vpc_security_group_egress_rule" "ecs_to_rds" {
  security_group_id = aws_security_group.ecs.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol = "tcp"
  from_port = 5432
  to_port = 5432
}
resource "aws_vpc_security_group_ingress_rule" "rds_from_ecs" {
  security_group_id = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.ecs.id
  ip_protocol = "tcp"
  from_port = 5432
  to_port = 5432
}
resource "aws_vpc_security_group_egress_rule" "workers_https" {
  security_group_id = aws_security_group.worker.id
  cidr_ipv4 = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port = 443
  to_port = 443
}
