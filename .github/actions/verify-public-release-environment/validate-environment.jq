def positive_integer:
  type == "number" and . > 0 and floor == .;

def exact_branch_policy_rule:
  (.protection_rules | type == "array" and length == 1)
  and (.protection_rules[0] | type == "object")
  and (.protection_rules[0] | keys == ["id", "node_id", "type"])
  and (.protection_rules[0].id | positive_integer)
  and (.protection_rules[0].node_id | type == "string" and length > 0)
  and .protection_rules[0].type == "branch_policy";

($repository | length == 1)
and ($repository[0].private == false)
and ($repository[0].visibility == "public")
and ($repository[0].owner.type == "User" or $repository[0].owner.type == "Organization")
and .name == "dockerhub-public"
and .can_admins_bypass == false
and .deployment_branch_policy == {
  "protected_branches": false,
  "custom_branch_policies": true
}
and exact_branch_policy_rule
and ($policies | length == 1)
and ($policies[0].total_count == 1)
and ($policies[0].branch_policies | type == "array" and length == 1)
and ($policies[0].branch_policies[0].name == "v*.*.*")
and ($policies[0].branch_policies[0].type == "tag")
