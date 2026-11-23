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
resource "aws_vpc_security_group_egress_rule" "workers_https" {
  security_group_id = aws_security_group.worker.id
  cidr_ipv4 = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port = 443
  to_port = 443
}
