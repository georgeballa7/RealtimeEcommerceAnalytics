output "vpc_id" {
  description = "Dedicated project VPC."
  value       = aws_vpc.ecommerce.id
}

output "database_subnet_ids" {
  description = "Subnets for the RDS database subnet group."
  value       = aws_subnet.database[*].id
}

output "rds_security_group_id" {
  description = "Database security group; allows approved Glue and other explicit task security groups on port 5432."
  value       = aws_security_group.rds.id
}

output "rds_endpoint" {
  description = "Private RDS endpoint used by workloads inside the VPC."
  value       = aws_db_instance.ecommerce.endpoint
}

output "rds_master_secret_arn" {
  description = "AWS-managed Secrets Manager secret containing the RDS master credentials."
  value       = aws_db_instance.ecommerce.master_user_secret[0].secret_arn
  sensitive   = true
}

output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "application_subnet_ids" { value = aws_subnet.application[*].id }
output "nat_gateway_ids" { value = aws_nat_gateway.ecommerce[*].id }
output "ecs_cluster_name" { value = aws_ecs_cluster.ecommerce.name }
output "event_queue_url" { value = aws_sqs_queue.events.url }
output "event_dlq_url" { value = aws_sqs_queue.events_dlq.url }
output "landing_bucket_name" { value = aws_s3_bucket.landing.bucket }
