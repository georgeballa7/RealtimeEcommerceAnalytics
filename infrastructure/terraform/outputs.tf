output "vpc_id" {
  description = "Dedicated project VPC."
  value       = aws_vpc.ecommerce.id
}

output "private_subnet_ids" {
  description = "Subnets for the RDS database subnet group."
  value       = aws_subnet.database[*].id
}

output "rds_security_group_id" {
  description = "Database security group, with no ingress rules initially."
  value       = aws_security_group.rds.id
}

output "rds_endpoint" {
  description = "Private RDS endpoint. Requires an approved connection path."
  value       = aws_db_instance.ecommerce.endpoint
}

output "rds_master_secret_arn" {
  description = "AWS-managed Secrets Manager secret containing the RDS master credentials."
  value       = aws_db_instance.ecommerce.master_user_secret[0].secret_arn
  sensitive   = true
}
