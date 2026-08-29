def required_reviewer_rules:
  [.protection_rules[]? | select(.type == "required_reviewers")];

def positive_integer:
  type == "number" and . > 0 and floor == .;

def approved_reviewer_set:
  if ($approved_reviewers | length == 1)
    and ($approved_reviewers[0] | type == "object")
    and ($approved_reviewers[0] | keys == ["reviewers", "schema"])
    and ($approved_reviewers[0].schema == "cardrag.public-release-reviewers.v1")
    and ($approved_reviewers[0].reviewers | type == "array")
    and ($approved_reviewers[0].reviewers | length >= 1 and length <= 6)
    and all(
      $approved_reviewers[0].reviewers[];
      type == "object"
      and (keys == ["id", "type"])
      and (.type == "User" or .type == "Team")
      and (.id | positive_integer)
    )
    and (
      [$approved_reviewers[0].reviewers[] | {type, id}]
      | unique_by([.type, .id])
      | length
    ) == ($approved_reviewers[0].reviewers | length)
  then
    [$approved_reviewers[0].reviewers[] | {type, id}] | sort_by([.type, .id])
  else
    null
  end;

def actual_reviewer_set:
  [
    required_reviewer_rules[0].reviewers[]
    | select(
      type == "object"
      and (keys == ["reviewer", "type"])
      and (.type == "User" or .type == "Team")
      and (.reviewer | type == "object")
      and (.reviewer.id | positive_integer)
    )
    | {type, id: .reviewer.id}
  ] as $normalized
  | if ($normalized | length) == (required_reviewer_rules[0].reviewers | length)
    then $normalized | sort_by([.type, .id])
    else null
    end;

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
and (required_reviewer_rules | length == 1)
and (required_reviewer_rules[0].prevent_self_review == true)
and (required_reviewer_rules[0].reviewers | type == "array")
and (required_reviewer_rules[0].reviewers | length >= 1 and length <= 6)
and (approved_reviewer_set != null)
and (actual_reviewer_set == approved_reviewer_set)
and ($policies | length == 1)
and ($policies[0].total_count == 1)
and ($policies[0].branch_policies | type == "array" and length == 1)
and ($policies[0].branch_policies[0].name == "v*.*.*")
and ($policies[0].branch_policies[0].type == "tag")
