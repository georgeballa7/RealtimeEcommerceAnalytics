data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "ecommerce" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "ecommerce-vpc" }
}

resource "aws_subnet" "database" {
  count = 2

  vpc_id                  = aws_vpc.ecommerce.id
  cidr_block              = var.private_subnet_cidrs[count.index]
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "ecommerce-db-private-${count.index + 1}" }
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.ecommerce.id

  # Intentionally local-only: no Internet Gateway or NAT route.
  tags = { Name = "ecommerce-db-private-rt" }
}

resource "aws_route_table_association" "database" {
  count = 2

  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}

resource "aws_db_subnet_group" "ecommerce" {
  name       = "ecommerce-db-subnets"
  subnet_ids = aws_subnet.database[*].id

  tags = { Name = "ecommerce-db-subnets" }
}

resource "aws_security_group" "rds" {
  name_prefix = "ecommerce-rds-"
  description = "PostgreSQL database; ingress intentionally closed until access path is approved"
  vpc_id      = aws_vpc.ecommerce.id

  # No inbound PostgreSQL rule and no open outbound rule. The approved
  # client security group can be authorized in a separate reviewed change.
  ingress = []
  egress  = []

  tags = { Name = "ecommerce-rds" }

  lifecycle {
    create_before_destroy = true
  }
}
