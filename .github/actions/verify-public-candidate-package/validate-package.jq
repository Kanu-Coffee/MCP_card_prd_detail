type == "object"
and (.id | type == "number" and . > 0 and floor == .)
and .name == ($ARGS.named.package_name // "mcp-card-prd-detail-candidate")
and .package_type == "container"
and .visibility == "public"
and (.owner | type == "object")
and (.owner.login | type == "string")
and ((.owner.login | ascii_downcase) == ($owner | ascii_downcase))
and (($ARGS.named.owner_type // "User") == "User" or ($ARGS.named.owner_type // "User") == "Organization")
and .owner.type == ($ARGS.named.owner_type // "User")
