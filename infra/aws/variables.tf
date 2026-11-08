variable "name" {
  description = "Name prefix for every resource (cluster, VPC, roles)."
  type        = string
  default     = "deploy-reference"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "name: 3-32 characters, lowercase letters, digits and hyphens."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-central-1"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Pick one in standard support (`aws eks describe-cluster-versions`); the cluster is created with upgrade_policy STANDARD, so it never drifts into paid extended support silently."
  type        = string
  default     = "1.35"
}

variable "vpc_cidr" {
  description = "VPC address range; split into two public and two private subnets."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "api_allowed_cidrs" {
  description = "Networks allowed to reach the public Kubernetes API endpoint (your office or VPN). No default: an open API endpoint must be a deliberate choice."
  type        = list(string)

  validation {
    # Canonical network address only (no host bits, no leading zeros), and not a private,
    # loopback, link-local, shared (CGNAT) or multicast range. 0.0.0.0/0 is allowed: opening
    # the endpoint to everyone is legal, but it has to be typed on purpose.
    condition = length(var.api_allowed_cidrs) > 0 && alltrue([
      for c in var.api_allowed_cidrs : can(cidrnetmask(c))
      && "${cidrhost(c, 0)}/${split("/", c)[1]}" == c
      && !can(regex("^(10|127)\\.|^192\\.168\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^169\\.254\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^(22[4-9]|2[3-5][0-9])\\.", c))
    ])
    error_message = "api_allowed_cidrs: public IPv4 networks in canonical form (e.g. 203.0.113.0/24); private, loopback, link-local, shared and multicast ranges are refused."
  }
}

variable "node_instance_types" {
  description = "Instance types for the managed node group."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_min" {
  description = "Minimum node count."
  type        = number
  default     = 1
}

variable "node_desired" {
  description = "Desired node count at creation."
  type        = number
  default     = 2
}

variable "node_max" {
  description = "Maximum node count."
  type        = number
  default     = 3

  validation {
    condition     = var.node_max >= 1 && var.node_max <= 10
    error_message = "node_max must be between 1 and 10 for a reference deployment."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for resources tagged with this project; alerts at 80% and 100% of it. Set it to what you expect to spend (control plane, nodes, NAT gateway); check current prices."
  type        = number
  default     = 200
}

variable "budget_alert_emails" {
  description = "E-mail addresses for the budget alerts. Empty: the budget exists but notifies nobody."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}
