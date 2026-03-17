#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
ROLLOVER_FUNDING="${LIQUIDITY_BUDGET:-200}"
RAMP_INTERVAL_MINUTES="${LIQUIDITY_RAMP_INTERVAL_MINUTES:-30}"
ROLLOVER_DAY_CAP="${ROLLOVER_DAY_CAP:-100}"
RESOLUTION_LOOKBACK_HOURS="${RESOLUTION_LOOKBACK_HOURS:-48}"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
NOW_EPOCH="$(date -u -d "${NOW}" +%s)"
TODAY="$(date -u +%Y-%m-%d)"
DEADLINE="$(date -u -d '+1 day' +%Y-%m-%dT00:00:00Z)"
LOOKBACK_CUTOFF_EPOCH="$(date -u -d "${RESOLUTION_LOOKBACK_HOURS} hours ago" +%s)"

log() {
    echo "[rollover] $*"
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log "Missing required command: $1"
        exit 1
    fi
}

urlencode() {
    jq -rn --arg value "$1" '$value | @uri'
}

api_get() {
    curl -sf "${API_URL}/v1$1"
}

admin_get() {
    curl -sf \
        -H "Authorization: Bearer ${FUTARCHY_ADMIN_KEY}" \
        "${API_URL}/v1$1"
}

admin_post() {
    local path="$1"
    if [ "$#" -gt 1 ]; then
        curl -sf -X POST \
            -H "Authorization: Bearer ${FUTARCHY_ADMIN_KEY}" \
            -H "Content-Type: application/json" \
            -d "$2" \
            "${API_URL}/v1${path}"
        return
    fi

    curl -sf -X POST \
        -H "Authorization: Bearer ${FUTARCHY_ADMIN_KEY}" \
        "${API_URL}/v1${path}"
}

admin_patch() {
    local path="$1"
    local body="$2"
    curl -sf -X PATCH \
        -H "Authorization: Bearer ${FUTARCHY_ADMIN_KEY}" \
        -H "Content-Type: application/json" \
        -d "${body}" \
        "${API_URL}/v1${path}"
}

refresh_open_markets() {
    api_get "/markets?category=pr_merge&status=open"
}

get_tracked_repos() {
    admin_get "/admin/repos" | jq -r '.[] | select(.enabled != false) | .repo'
}

resolve_treasury_account_id() {
    if [ -n "${FUTARCHY_TREASURY_ID:-}" ]; then
        echo "${FUTARCHY_TREASURY_ID}"
        return 0
    fi

    local first_market_id
    first_market_id="$(api_get "/markets?category=pr_merge" | jq -r '.[0].market_id // empty')"
    if [ -z "${first_market_id}" ]; then
        return 0
    fi

    api_get "/markets/${first_market_id}" | jq -r '
        .metadata.funding_account_id //
        .metadata.treasury_account_id //
        empty
    '
}

day_cap_reached() {
    local repo="$1"
    local pr_num="$2"
    local prefix encoded dates streak previous expected

    prefix="${repo}#${pr_num}"
    encoded="$(urlencode "${prefix}")"
    dates="$(
        api_get "/markets?category=pr_merge&category_id=${encoded}" | \
            jq -r '.[].category_id | split("@")[1] // empty' | \
            sort -ru
    )"

    if [ -z "${dates}" ]; then
        return 1
    fi

    streak=0
    previous=""

    while IFS= read -r market_date; do
        [ -z "${market_date}" ] && continue

        if [ "${streak}" -eq 0 ]; then
            streak=1
            previous="${market_date}"
            continue
        fi

        expected="$(date -u -d "${previous} -1 day" +%Y-%m-%d)"
        if [ "${market_date}" != "${expected}" ]; then
            break
        fi

        streak=$((streak + 1))
        previous="${market_date}"
    done <<< "${dates}"

    [ "${streak}" -ge "${ROLLOVER_DAY_CAP}" ]
}

resolve_recent_closed_prs() {
    local tracked_repos="$1"
    local open_markets="$2"

    log "Checking recently closed PRs before voiding expired markets"

    while IFS= read -r repo; do
        [ -z "${repo}" ] && continue

        local closed_prs
        if ! closed_prs="$(gh pr list \
            --repo "${repo}" \
            --state closed \
            --json number,title,mergedAt,closedAt \
            --limit 50)"; then
            log "Failed to list closed PRs for ${repo}; skipping resolution check"
            continue
        fi

        while IFS= read -r pr; do
            [ -z "${pr}" ] && continue

            local pr_num merged_at event_at outcome event_epoch prefix
            pr_num="$(jq -r '.number' <<< "${pr}")"
            merged_at="$(jq -r '.mergedAt // empty' <<< "${pr}")"
            event_at="$(jq -r '(.mergedAt // .closedAt // empty)' <<< "${pr}")"
            outcome="no"
            if [ -n "${merged_at}" ]; then
                outcome="yes"
            fi

            event_epoch="$(date -u -d "${event_at}" +%s)"
            if [ "${event_epoch}" -lt "${LOOKBACK_CUTOFF_EPOCH}" ]; then
                continue
            fi

            prefix="${repo}#${pr_num}"
            while IFS= read -r market; do
                [ -z "${market}" ] && continue

                local market_id deadline deadline_epoch
                market_id="$(jq -r '.market_id' <<< "${market}")"
                deadline="$(jq -r '.deadline // empty' <<< "${market}")"

                if [ -n "${deadline}" ]; then
                    deadline_epoch="$(date -u -d "${deadline}" +%s)"
                    if [ "${event_epoch}" -gt "${deadline_epoch}" ]; then
                        log "Leaving market ${market_id} open for void: ${prefix} closed after deadline"
                        continue
                    fi
                fi

                log "Resolving market ${market_id} for ${prefix} as ${outcome}"
                admin_post "/admin/markets/${market_id}/resolve" "$(jq -n --arg outcome "${outcome}" '{outcome: $outcome}')" >/dev/null
            done < <(jq -c --arg prefix "${prefix}" '.[] | select(.category_id | startswith($prefix))' <<< "${open_markets}")
        done < <(
            jq -c '.[] | select((.mergedAt // .closedAt // null) != null)' <<< "${closed_prs}"
        )
    done <<< "${tracked_repos}"
}

void_expired_markets() {
    local open_markets="$1"

    log "Voiding expired open markets"

    while IFS= read -r market; do
        [ -z "${market}" ] && continue

        local market_id category_id deadline deadline_epoch
        market_id="$(jq -r '.market_id' <<< "${market}")"
        category_id="$(jq -r '.category_id' <<< "${market}")"
        deadline="$(jq -r '.deadline // empty' <<< "${market}")"

        [ -z "${deadline}" ] && continue

        deadline_epoch="$(date -u -d "${deadline}" +%s)"
        if [ "${deadline_epoch}" -gt "${NOW_EPOCH}" ]; then
            continue
        fi

        log "Voiding expired market ${market_id} (${category_id})"
        admin_post "/admin/markets/${market_id}/void" >/dev/null
    done < <(jq -c '.[]' <<< "${open_markets}")
}

create_rollover_markets() {
    local tracked_repos="$1"
    local treasury_id="$2"
    local open_markets="$3"

    if [ -z "${treasury_id}" ]; then
        log "FUTARCHY_TREASURY_ID is not set and could not be inferred; skipping new market creation"
        return 0
    fi

    log "Ensuring each open PR has today's market"

    while IFS= read -r repo; do
        [ -z "${repo}" ] && continue

        local open_prs
        if ! open_prs="$(gh pr list \
            --repo "${repo}" \
            --state open \
            --json number,title \
            --limit 100)"; then
            log "Failed to list open PRs for ${repo}; skipping market creation"
            continue
        fi

        while IFS= read -r pr; do
            [ -z "${pr}" ] && continue

            local pr_num pr_title category_id existing_count body question
            pr_num="$(jq -r '.number' <<< "${pr}")"
            pr_title="$(jq -r '.title' <<< "${pr}")"
            category_id="${repo}#${pr_num}@${TODAY}"

            existing_count="$(jq --arg category_id "${category_id}" '
                map(select(.category_id == $category_id)) | length
            ' <<< "${open_markets}")"

            if [ "${existing_count}" -gt 0 ]; then
                log "Market already exists for ${category_id}"
                continue
            fi

            if day_cap_reached "${repo}" "${pr_num}"; then
                log "Skipping ${repo}#${pr_num}: reached ${ROLLOVER_DAY_CAP}-day rollover cap"
                continue
            fi

            question="Will PR #${pr_num} '${pr_title}' merge?"
            body="$(jq -n \
                --arg question "${question}" \
                --arg category_id "${category_id}" \
                --arg deadline "${DEADLINE}" \
                --arg funding "${ROLLOVER_FUNDING}" \
                --argjson funding_account_id "${treasury_id}" \
                --argjson pr_number "${pr_num}" \
                --arg repo "${repo}" \
                '{
                    question: $question,
                    category: "pr_merge",
                    category_id: $category_id,
                    deadline: $deadline,
                    funding: $funding,
                    funding_account_id: $funding_account_id,
                    metadata: {
                        market_type: "conditional",
                        pr_number: $pr_number,
                        repo: $repo,
                        funding_account_id: $funding_account_id,
                        resolution_rules: "YES if merged before deadline, NO if closed without merge before deadline, VOID otherwise",
                        liquidity_steps_remaining: 0,
                        next_liquidity_at: null
                    }
                }')"

            log "Creating rollover market for ${category_id}"
            admin_post "/admin/markets" "${body}" >/dev/null
        done < <(jq -c '.[]' <<< "${open_prs}")
    done <<< "${tracked_repos}"
}

run_liquidity_ramp() {
    local treasury_id="$1"
    local open_markets="$2"

    if [ -z "${treasury_id}" ]; then
        log "FUTARCHY_TREASURY_ID is not set and could not be inferred; skipping liquidity ramp"
        return 0
    fi

    log "Applying pending liquidity ramp steps"

    while IFS= read -r market; do
        [ -z "${market}" ] && continue

        local market_id detail steps_remaining next_liquidity_at step_amount
        local next_epoch new_steps metadata_body new_next
        market_id="$(jq -r '.market_id' <<< "${market}")"
        detail="$(api_get "/markets/${market_id}")"
        steps_remaining="$(jq -r '.metadata.liquidity_steps_remaining // 0' <<< "${detail}")"
        next_liquidity_at="$(jq -r '.metadata.next_liquidity_at // empty' <<< "${detail}")"
        step_amount="$(jq -r '.metadata.liquidity_step // empty' <<< "${detail}")"

        if [ "${steps_remaining}" -le 0 ] || [ -z "${next_liquidity_at}" ] || [ -z "${step_amount}" ]; then
            continue
        fi

        next_epoch="$(date -u -d "${next_liquidity_at}" +%s)"
        if [ "${next_epoch}" -gt "${NOW_EPOCH}" ]; then
            continue
        fi

        log "Adding ${step_amount} credits to market ${market_id}"
        admin_post "/admin/markets/${market_id}/add-liquidity" "$(jq -n \
            --arg amount "${step_amount}" \
            --argjson funding_account_id "${treasury_id}" \
            '{amount: $amount, funding_account_id: $funding_account_id}')" >/dev/null

        new_steps=$((steps_remaining - 1))
        if [ "${new_steps}" -gt 0 ]; then
            new_next="$(date -u -d "${next_liquidity_at} +${RAMP_INTERVAL_MINUTES} minutes" +%Y-%m-%dT%H:%M:%SZ)"
            metadata_body="$(jq -n \
                --argjson steps "${new_steps}" \
                --arg next_liquidity_at "${new_next}" \
                '{metadata: {liquidity_steps_remaining: $steps, next_liquidity_at: $next_liquidity_at}}')"
        else
            metadata_body="$(jq -n \
                '{metadata: {liquidity_steps_remaining: 0, next_liquidity_at: null}}')"
        fi

        admin_patch "/admin/markets/${market_id}/metadata" "${metadata_body}" >/dev/null
        log "Market ${market_id}: liquidity ramp updated (${new_steps} steps remaining)"
    done < <(jq -c '.[]' <<< "${open_markets}")
}

main() {
    require_command curl
    require_command date
    require_command gh
    require_command jq

    if [ -z "${FUTARCHY_ADMIN_KEY:-}" ]; then
        log "FUTARCHY_ADMIN_KEY is not set"
        exit 1
    fi

    log "Starting server-side rollover at ${NOW}"

    local tracked_repos open_markets treasury_id
    tracked_repos="$(get_tracked_repos)"
    open_markets="$(refresh_open_markets)"

    resolve_recent_closed_prs "${tracked_repos}" "${open_markets}"

    open_markets="$(refresh_open_markets)"
    void_expired_markets "${open_markets}"

    treasury_id="$(resolve_treasury_account_id)"
    if [ -n "${treasury_id}" ]; then
        log "Using treasury account ${treasury_id}"
    else
        log "Treasury account not configured in environment or market metadata"
    fi

    open_markets="$(refresh_open_markets)"
    create_rollover_markets "${tracked_repos}" "${treasury_id}" "${open_markets}"

    open_markets="$(refresh_open_markets)"
    run_liquidity_ramp "${treasury_id}" "${open_markets}"

    log "Rollover complete"
}

main "$@"
