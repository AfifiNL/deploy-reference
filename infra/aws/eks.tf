resource "aws_kms_key" "eks" {
  description             = "${var.name}: envelope encryption of Kubernetes secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_cloudwatch_log_group" "eks" {
  # EKS writes control-plane logs here; creating it first sets the retention.
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 14
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids              = concat(aws_subnet.private[*].id, aws_subnet.public[*].id)
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.api_allowed_cidrs
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.eks.arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  upgrade_policy {
    support_type = "STANDARD"
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.eks,
  ]
}

# Launch template: IMDSv2 only with a hop limit of 1 (pods cannot reach the node's instance
# metadata), an encrypted root volume, and the project tags on instances and volumes so the
# cost budget sees them (default_tags do not reach instances a node group launches).
resource "aws_launch_template" "nodes" {
  name_prefix = "${var.name}-nodes-"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 30
      volume_type = "gp3"
      encrypted   = true
    }
  }

  dynamic "tag_specifications" {
    for_each = ["instance", "volume"]
    content {
      resource_type = tag_specifications.value
      tags = merge(var.tags, {
        project    = var.name
        managed-by = "terraform"
        Name       = "${var.name}-node"
      })
    }
  }
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_STANDARD"
  instance_types  = var.node_instance_types

  launch_template {
    id      = aws_launch_template.nodes.id
    version = aws_launch_template.nodes.latest_version
  }

  scaling_config {
    min_size     = var.node_min
    desired_size = var.node_desired
    max_size     = var.node_max
  }

  update_config {
    max_unavailable = 1
  }

  lifecycle {
    # The cluster autoscaler (or a person) changes the desired size; don't fight it.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni]
}

# Add-on versions: the default version EKS recommends for this Kubernetes version, resolved at
# plan time and shown in the plan, so an upgrade is a reviewed change.
data "aws_eks_addon_version" "this" {
  for_each = toset(local.addons)

  addon_name         = each.value
  kubernetes_version = var.kubernetes_version # known at plan time, so versions show in the plan
}

locals {
  addons = ["vpc-cni", "coredns", "kube-proxy", "metrics-server", "eks-pod-identity-agent"]
}

# Networking add-ons come BEFORE the nodes: the node role has no CNI permissions, so the VPC
# CNI must already run with its own Pod Identity role when the first node joins, or the nodes
# never become Ready. DaemonSet add-ons become active with zero nodes.
resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  addon_version               = data.aws_eks_addon_version.this["eks-pod-identity-agent"].version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  addon_version               = data.aws_eks_addon_version.this["vpc-cni"].version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  pod_identity_association {
    role_arn        = aws_iam_role.vpc_cni.arn
    service_account = "aws-node"
  }

  depends_on = [aws_eks_addon.pod_identity_agent, aws_iam_role_policy_attachment.vpc_cni]
}

# Add-ons that need nodes to run on come after the node group.
resource "aws_eks_addon" "core" {
  for_each = toset(["coredns", "kube-proxy", "metrics-server"])

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  addon_version               = data.aws_eks_addon_version.this[each.value].version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.default]
}
