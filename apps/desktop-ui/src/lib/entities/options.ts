export const countryOptions = [
  { code: "NG", label: "Nigeria", timezone: "Africa/Lagos" },
  { code: "GH", label: "Ghana", timezone: "Africa/Accra" },
  { code: "KE", label: "Kenya", timezone: "Africa/Nairobi" },
  { code: "ZA", label: "South Africa", timezone: "Africa/Johannesburg" },
  { code: "AE", label: "United Arab Emirates", timezone: "Asia/Dubai" },
  { code: "GB", label: "United Kingdom", timezone: "Europe/London" },
  { code: "US", label: "United States", timezone: "America/New_York" },
] as const;

export const timezoneOptions = [
  "Africa/Lagos",
  "Africa/Accra",
  "Africa/Nairobi",
  "Africa/Johannesburg",
  "Asia/Dubai",
  "Europe/London",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
] as const;

export const accountingStandardOptions = [
  "IFRS",
  "US GAAP",
  "UK GAAP",
  "IPSAS",
  "Local GAAP",
  "Other",
] as const;

export const commonWorkspaceRoleOptions = [
  { label: "Owner", value: "owner" },
  { label: "Controller", value: "controller" },
  { label: "Reviewer", value: "reviewer" },
  { label: "Preparer", value: "preparer" },
  { label: "Observer", value: "observer" },
] as const;
