resource "aws_sns_topic" "alerts" {
  name = "ecommerce-${var.environment}-infra-alerts"
}

# SNS subscriptions are added separately after recipients approve them.
resource "aws_cloudwatch_metric_alarm" "dlq_nonempty" {
  alarm_name          = "ecommerce-${var.environment}-dlq-nonempty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions = { QueueName = aws_sqs_queue.events_dlq.name }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "queue_oldest_age" {
  alarm_name          = "ecommerce-${var.environment}-queue-age"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 300
  comparison_operator = "GreaterThanThreshold"
  dimensions = { QueueName = aws_sqs_queue.events.name }
  treat_missing_data  = "notBreaching"
  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "ecommerce-${var.environment}-rds-high-cpu"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions = { DBInstanceIdentifier = aws_db_instance.ecommerce.id }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
