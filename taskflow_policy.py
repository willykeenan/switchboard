"""Local task-bound decision policy. Hosted AgentBrain is a separate product.

This is the policy component, not the authoritative host adapter. ``checked``
must come from that adapter's canonical task/permission/capability observations;
JSON declarations supplied by a worker are not checked facts. The TaskFlow host
binds the existing-turn judgment path; executable and observation adapters remain
unbound. All results explicitly confer no execution authority.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid


BUDGET_KEYS = (
    'maxObservationCycles', 'maxJudgmentTurns', 'maxCandidateRevisions',
    'maxWallSeconds', 'maxModelCalls', 'maxCpuSeconds', 'maxMemoryBytes',
    'maxProcesses', 'maxOutputBytes',
)
THRESHOLDS = ('minimumSupportCases', 'minimumLowerSuccessBound',
              'maximumUncertainty', 'minimumScoreMargin')
ACTIVE_STATES = {'QUALIFIED', 'SHADOW', 'LIMITED_USE', 'ACTIVE'}


class DecisionConflict(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise DecisionConflict(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def budget(value):
    require(isinstance(value, dict) and set(value) == set(BUDGET_KEYS),
            'Every original-task budget must be explicit')
    require(all(type(value[key]) is int and value[key] >= 0 for key in BUDGET_KEYS),
            'Budgets must be finite nonnegative integers')
    return value


def timestamp(value):
    require(isinstance(value, str), 'Explicit timestamp required')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(date.tzinfo is not None, 'Timezone required')
        return date.timestamp()
    except (ValueError, OverflowError) as error:
        raise DecisionConflict('Invalid timestamp') from error


def number(value, name, lower=0, upper=1):
    require(type(value) in (int, float) and math.isfinite(value)
            and lower <= value <= upper, 'Invalid numeric policy evidence: ' + name)
    return value


def affordable(charge, remaining):
    budget(charge)
    return all(charge[key] <= remaining[key] for key in BUDGET_KEYS)


def validate_thresholds(value):
    require(isinstance(value, dict) and set(value) == set(THRESHOLDS),
            'Task-specific thresholds must be explicit')
    require(type(value['minimumSupportCases']) is int and value['minimumSupportCases'] >= 1,
            'Positive support threshold required')
    for key in THRESHOLDS[1:]:
        number(value[key], key)
    return value


def _result(request, remaining, disposition, reason, **fields):
    return {
        'schemaVersion': 'ke.agentbrain.decision-result.v2',
        'fixture': request['fixture'], 'executionAuthority': False,
        'decisionId': request['decisionId'], 'task': request['task'],
        'disposition': disposition, 'reason': reason,
        'authorityStatus': 'not-applicable' if disposition == 'HOLD' else 'bound',
        'remainingBudget': dict(remaining), 'learningEpoch': request['learningEpoch'],
        **fields,
    }


def _hold(request, remaining, reason):
    return _result(request, remaining, 'HOLD', reason, resumeDependency=reason)


def _judgment(request, checked, remaining, reason):
    path = checked.get('judgment')
    if path is None:
        return _hold(request, remaining, reason + '; no judgment route')
    mode = path.get('mode')
    require(mode in ('judgment-only', 'tool-assisted'), 'Explicit judgment mode required')
    if remaining['maxJudgmentTurns'] < 1 or remaining['maxModelCalls'] < 1:
        return _hold(request, remaining, 'Original task judgment budget exhausted')
    charge = path['charge']
    require(charge['maxJudgmentTurns'] >= 1 and charge['maxModelCalls'] >= 1,
            'Judgment cannot declare zero turns or zero model calls')
    if not affordable(charge, remaining):
        return _hold(request, remaining, 'Original task judgment resource budget exhausted')
    tools = path.get('toolBindingRefs', [])
    if mode == 'judgment-only' and tools:
        return _hold(request, remaining, 'Judgment-only cannot use undeclared tools')
    if mode == 'tool-assisted' and (not tools or path.get('allToolsVerified') is not True):
        return _hold(request, remaining, 'Every judgment tool needs its own current binding')
    if path.get('inputAccessVerified') is not True:
        return _hold(request, remaining, 'Judgment input access is not verified')
    fields = {'question': path['question'], 'judgmentMode': mode}
    if tools:
        fields['judgmentToolBindingRefs'] = tools
    if path.get('authorizationVerified') is True and path.get('adapterVerified') is True:
        fields.update(judgmentAuthorityRef=path['authorityRef'],
                      judgmentAdapterRef=path['adapterRef'])
    else:
        fields['authorityStatus'] = 'pending-authorization'
    return _result(request, remaining, 'NEEDS_JUDGMENT', reason, **fields)


def select(request, checked, policy, *, now):
    """Select from independently checked facts; never execute or reserve budget.

    Production integration must resolve the facts and reserve the selected charge
    inside its existing task transaction. A pending request is not an attempt.
    """
    require(type(now) in (int, float) and math.isfinite(now), 'Finite current time required')
    require(request['schemaVersion'] == 'ke.agentbrain.decision-request.v2'
            and request['executionAuthority'] is False and type(request['fixture']) is bool,
            'Exact non-authorizing decision request required')
    require(str(uuid.UUID(request['decisionId'])) == request['decisionId'], 'UUID decision identity required')
    remaining = budget(checked['remainingBudget'])
    budget(request['budget'])
    require(all(remaining[k] <= request['budget'][k] for k in BUDGET_KEYS),
            'Current budgets cannot exceed the original task budget')
    validate_thresholds(policy)
    require(type(request['learningEpoch']) is int and request['learningEpoch'] >= 0,
            'Exact nonnegative learning epoch required')
    if (checked.get('task') != request['task'] or checked.get('taskCurrent') is not True
            or checked.get('taskAuthorityVerified') is not True or checked.get('cancelled') is not False):
        return _hold(request, remaining, 'Original task is invalid, cancelled or unauthorized')
    if checked.get('learningEpoch') != request['learningEpoch']:
        return _hold(request, remaining, 'Learning epoch changed; prepare a new decision')
    if remaining['maxWallSeconds'] == 0 or now >= checked['taskDeadline']:
        return _hold(request, remaining, 'Original task wall-time budget exhausted')
    require(type(checked['taskDeadline']) in (int, float) and math.isfinite(checked['taskDeadline']),
            'Finite original-task deadline required')
    observation = request['observation']
    fresh = (checked.get('observationCurrent') is True
             and checked.get('observationHash') == digest(observation)
             and timestamp(observation['observedAt']) <= now < timestamp(observation['validUntil']))
    missing = observation['missingFields']
    require(isinstance(missing, list) and all(isinstance(x, str) and x for x in missing),
            'Missing observation fields must be explicit')
    candidates = checked['candidates']
    require(isinstance(candidates, list) and len(candidates) <= 100,
            'Bounded checked candidate set required')
    identities = [c['capability']['capabilityId'] for c in candidates]
    require(len(set(identities)) == len(identities), 'Ambiguous capability identity')
    executable, measurements = [], []
    for c in candidates:
        require(c['action'] in ('EXECUTE', 'OBSERVE'), 'Unknown executable action')
        if (c['state'] not in ACTIVE_STATES or c.get('lifecycleUsePermitted') is not True
                or any(c.get(k) is not True for k in
                       ('qualified', 'applicable', 'compatible', 'permissionVerified',
                        'sourceCurrent', 'dependenciesCurrent', 'profileEnforced',
                        'evaluatorIndependent', 'calibrated'))):
            continue
        # A profile label is insufficient: the host must check its live backend
        # attestation and every-run enforcement before setting profileEnforced.
        if not c.get('profileRef') or not c.get('manifestRef') or not c.get('authorityRef'):
            continue
        if not (type(c['supportCases']) is int and c['supportCases'] >= policy['minimumSupportCases']
                and number(c['lowerSuccessBound'], 'lowerSuccessBound') >= policy['minimumLowerSuccessBound']
                and number(c['uncertainty'], 'uncertainty') <= policy['maximumUncertainty']):
            continue
        number(c['score'], 'score')
        require(type(c['validUntil']) in (int, float) and math.isfinite(c['validUntil']),
                'Finite capability expiry required')
        if c['validUntil'] <= now or not affordable(c['charge'], remaining):
            continue
        if c['action'] == 'OBSERVE':
            require(c['charge']['maxObservationCycles'] >= 1, 'Observation must charge a cycle')
            if (not fresh or missing) and c.get('resolvesObservationGap') is True:
                measurements.append(c)
        elif fresh and not missing:
            executable.append(c)
    eligible = measurements if not fresh or missing else executable
    eligible.sort(key=lambda c: (-c['score'], c['capability']['capabilityId']))
    if eligible:
        # A tie remains ambiguous even when the configured score margin is zero.
        ambiguous = (len(eligible) > 1 and
                     (eligible[0]['score'] == eligible[1]['score'] or
                      eligible[0]['score'] - eligible[1]['score'] < policy['minimumScoreMargin']))
        if not ambiguous:
            selected = eligible[0]
            return _result(request, remaining, selected['action'],
                           'Current permitted capability clears the task evidence thresholds',
                           selectedCapability=selected['capability'], manifestRef=selected['manifestRef'],
                           executionProfileRef=selected['profileRef'], authorityRef=selected['authorityRef'],
                           observationRef=observation['artifactRef'],
                           validUntil=datetime.fromtimestamp(min(selected['validUntil'], checked['taskDeadline'],
                               timestamp(observation['validUntil']) if selected['action'] == 'EXECUTE' else selected['validUntil']),
                               timezone.utc).isoformat(),
                           selectionDistribution=[{'capabilityId': selected['capability']['capabilityId'], 'probability': 1.0}])
        return _judgment(request, checked, remaining, 'Eligible choices conflict under the task policy')
    return _judgment(request, checked, remaining,
                     'Observation is missing or stale' if not fresh or missing
                     else 'No executable candidate clears every current evidence and authority gate')


def revalidate(request, sealed_result, checked, policy, *, now):
    """Reject a changed choice; no fallback/reselection at the admission boundary."""
    current = select(request, checked, policy, now=now)
    # Remaining wall time legitimately decreases between preparation and pickup.
    # Selection has just rechecked affordability; this cannot replenish the
    # original budget or select a different action/capability/authority.
    comparable = {**sealed_result, 'remainingBudget': current['remainingBudget']}
    require(digest(current) == digest(comparable), 'Decision changed; reseal before admission')
    require(current['disposition'] != 'HOLD' and current['authorityStatus'] == 'bound',
            'This decision cannot be admitted')
    return current
