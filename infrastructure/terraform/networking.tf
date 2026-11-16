data "aws_availability_zones" "available" { state = "available" }

resource "aws_vpc" "ecommerce" {
  cidr_block = var.vpc_cidr
  enable_dns_support = true
  enable_dns_hostnames = true
  tags = { Name = "ecommerce-vpc" }
}
resource "aws_internet_gateway" "ecommerce" {
  vpc_id = aws_vpc.ecommerce.id
  tags = { Name = "ecommerce-igw" }
}
resource "aws_subnet" "public" {
  count = 2
  vpc_id = aws_vpc.ecommerce.id
  cidr_block = var.public_subnet_cidrs[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  tags = { Name = "ecommerce-public-${count.index + 1}" }
}
resource "aws_subnet" "application" {
  count = 2
  vpc_id = aws_vpc.ecommerce.id
  cidr_block = var.application_subnet_cidrs[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  tags = { Name = "ecommerce-app-private-${count.index + 1}" }
}
resource "aws_subnet" "database" {
  count = 2
  vpc_id = aws_vpc.ecommerce.id
  cidr_block = var.private_subnet_cidrs[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  tags = { Name = "ecommerce-db-isolated-${count.index + 1}" }
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.ecommerce.id
  tags = { Name = "ecommerce-public-rt" }
}
resource "aws_route" "public" {
  route_table_id = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id = aws_internet_gateway.ecommerce.id
}
resource "aws_route_table_association" "public" {
  count = 2
  route_table_id = aws_route_table.public.id
  subnet_id = aws_subnet.public[count.index].id
}
resource "aws_eip" "nat" {
  count = 2
  domain = "vpc"
  depends_on = [aws_internet_gateway.ecommerce]
  tags = { Name = "ecommerce-nat-ip-${count.index + 1}" }
}
resource "aws_nat_gateway" "ecommerce" {
  count = 2
  allocation_id = aws_eip.nat[count.index].id
  subnet_id = aws_subnet.public[count.index].id
  depends_on = [aws_route.public]
  tags = { Name = "ecommerce-nat-${count.index + 1}" }
}
resource "aws_route_table" "application" {
  count = 2
  vpc_id = aws_vpc.ecommerce.id
  tags = { Name = "ecommerce-app-rt-${count.index + 1}" }
}
resource "aws_route" "application" {
  count = 2
  route_table_id = aws_route_table.application[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id = aws_nat_gateway.ecommerce[count.index].id
}
resource "aws_route_table_association" "application" {
  count = 2
  route_table_id = aws_route_table.application[count.index].id
  subnet_id = aws_subnet.application[count.index].id
}
# Isolated database subnets have only the VPC-local route.
resource "aws_route_table" "database" {
  vpc_id = aws_vpc.ecommerce.id
  tags = { Name = "ecommerce-db-isolated-rt" }
}
resource "aws_route_table_association" "database" {
  count = 2
  route_table_id = aws_route_table.database.id
  subnet_id = aws_subnet.database[count.index].id
}
resource "aws_db_subnet_group" "ecommerce" {
  name = "ecommerce-db-subnets"
  subnet_ids = aws_subnet.database[*].id
  tags = { Name = "ecommerce-db-subnets" }
}
resource "aws_vpc_endpoint" "s3" {
  vpc_id = aws_vpc.ecommerce.id
  service_name = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids = aws_route_table.application[*].id
}
