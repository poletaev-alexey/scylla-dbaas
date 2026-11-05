# TFLint configuration used by CI for every stack under infra/ (`tflint --recursive`).
# Kept in .github/ because it is CI policy; run locally with
#   cd infra && tflint --init --config "$PWD/../.github/tflint.hcl" \
#     && tflint --recursive --config "$PWD/../.github/tflint.hcl"

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.50.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
