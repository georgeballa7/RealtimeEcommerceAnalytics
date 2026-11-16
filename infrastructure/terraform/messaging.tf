resource "aws_sqs_queue" "events_dlq" {
  name = "ecommerce-${var.environment}-events-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled = true
}
resource "aws_sqs_queue" "events" {
  name = "ecommerce-${var.environment}-events"
  receive_wait_time_seconds = 20
  message_retention_seconds = 1209600
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  sqs_managed_sse_enabled = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.events_dlq.arn
    maxReceiveCount = 5
  })
}
resource "aws_sqs_queue_redrive_allow_policy" "events_dlq" {
  queue_url = aws_sqs_queue.events_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns = [aws_sqs_queue.events.arn]
  })
}
