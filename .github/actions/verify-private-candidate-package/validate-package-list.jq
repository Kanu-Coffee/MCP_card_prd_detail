if type == "array" and all(.[]; type == "array") then
  [
    .[][]
    | select(.name == "mcp-card-prd-detail-candidate")
  ] as $matches
  | ($matches | length == 1)
    and ($matches[0].package_type == "container")
    and ($matches[0].visibility == "private")
    and (($matches[0].owner.login | ascii_downcase) == ($owner | ascii_downcase))
    and ($matches[0].repository.full_name == $repository)
else
  false
end
