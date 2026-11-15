# Transactional outbox publisher

Publish committed RDS outbox records to Kinesis with retries and safe delivery-state transitions. PostgreSQL and Kinesis are not one distributed ACID transaction. **Not implemented yet.**
