package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/** Mirrors backend/tenant_api/app/api/license.py's EntitlementOut exactly. */
@Serializable
data class EntitlementOut(
    @SerialName("entitlement_code") val entitlementCode: String,
    @SerialName("value_type") val valueType: String,
    @SerialName("limit_numeric") val limitNumeric: Int? = null,
    @SerialName("enabled_boolean") val enabledBoolean: Boolean? = null,
    @SerialName("value_json") val valueJson: JsonElement? = null,
)

/** Mirrors QuotaUsageOut exactly. */
@Serializable
data class QuotaUsageOut(
    @SerialName("quota_code") val quotaCode: String,
    @SerialName("limit_value") val limitValue: Int,
    @SerialName("reserved_value") val reservedValue: Int,
    @SerialName("consumed_value") val consumedValue: Int,
)

/** Mirrors LicenseOut exactly - `status` is one of scheduled/active/grace/suspended/
 * expired/revoked, freshly synced server-side on every read
 * (csense_shared.licensing.lifecycle), never stale by the time this app sees it. */
@Serializable
data class LicenseOut(
    val id: String,
    @SerialName("plan_code") val planCode: String,
    @SerialName("plan_name") val planName: String,
    val status: String,
    @SerialName("starts_at") val startsAt: String,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("grace_ends_at") val graceEndsAt: String? = null,
    val entitlements: List<EntitlementOut> = emptyList(),
    @SerialName("quota_usage") val quotaUsage: List<QuotaUsageOut> = emptyList(),
)
