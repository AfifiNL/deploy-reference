# EKS for the reference deployment (validated, never applied)

Terraform for a small EKS cluster to run the same Helm chart in AWS: a VPC with two public and
two private subnets and flow logs, one NAT gateway, an EKS cluster (API authentication mode,
secrets encrypted with a KMS key, control-plane logs kept 14 days, upgrade policy STANDARD), a
managed node group of 1 to 3 nodes in the private subnets (IMDSv2 only, hop limit 1, encrypted
volumes, cost tags), the core add-ons (VPC CNI on its own Pod Identity role, CoreDNS,
kube-proxy, metrics-server, Pod Identity agent), and a monthly cost budget with optional
e-mail alerts.

**Status: `terraform validate` and `tflint` (AWS ruleset) pass in CI. It has never been planned
or applied against an AWS account.** Expect to fix details the first time you apply it.

## Before you apply

1. An AWS account you are allowed to spend money in, with credentials in your shell.
2. A remote state backend (S3 with locking) in `versions.tf` if anyone else will ever apply it.
3. Your own public network in `api_allowed_cidrs`, in canonical form; there is no default on
   purpose, and private, loopback, link-local, shared (CGNAT) and multicast ranges are refused.
4. A `kubernetes_version` that is in standard support today (`aws eks describe-cluster-versions`).
   With upgrade policy STANDARD, AWS upgrades the cluster when standard support ends; bump the
   variable to match afterwards, or the next plan will try to go back.
5. Activate the `project` cost-allocation tag in the billing console, or the budget sees nothing.

```bash
cd infra/aws
terraform init
terraform plan -var 'api_allowed_cidrs=["203.0.113.0/24"]' -out tfplan
terraform apply tfplan
$(terraform output -raw kubeconfig_command)
```

Then push the image to a registry the nodes can pull from (ECR), set `image.repository` and
`image.digest`, and install the chart with the default `values.yaml` (not `values-kind.yaml`).

## Cost

The EKS control plane, the nodes, the NAT gateway and its traffic, the flow logs and the KMS key
are billed while they exist. Set `monthly_budget_usd` to what you expect to spend; the default
(200) is a placeholder, not an estimate. The budget only notifies; it stops nothing. Tear everything down after a demo:

```bash
terraform destroy -var 'api_allowed_cidrs=["203.0.113.0/24"]'
```
