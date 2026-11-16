resource "aws_db_instance" "ecommerce" {
  identifier = "ecommerce-postgres-dev"

  engine         = "postgres"
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type       = "gp3"
  storage_encrypted  = true

  db_name                     = var.db_name
  username                    = var.db_master_username
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.ecommerce.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 1
  copy_tags_to_snapshot   = true
  deletion_protection     = true
  skip_final_snapshot     = true

  auto_minor_version_upgrade = true
  apply_immediately          = false

  tags = { Name = "ecommerce-postgres-dev" }
}
