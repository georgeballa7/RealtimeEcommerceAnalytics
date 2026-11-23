# No CloudFormation stack imports: stacks and their children remain CloudFormation-owned.
# This optional account-level SSM document import is separate.

import {
  to = aws_ssm_document.session_manager_preferences
  id = "SSM-SessionManagerRunShell"
}
