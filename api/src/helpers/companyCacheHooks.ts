import {
  onCatalogChanged,
  onQueuesChanged
} from "../services/CacheServices/CacheInvalidationService";

type CompanyCarrier = { companyId?: number | string | null };
type BulkOptions = { where?: { companyId?: unknown } };

const companyIdOf = (value: unknown): number | null => {
  const companyId = Number(value);
  if (!Number.isFinite(companyId) || companyId <= 0) return null;
  return companyId;
};

export const invalidateCatalogInstance = (instance?: CompanyCarrier): void => {
  const companyId = companyIdOf(instance?.companyId);
  if (companyId) onCatalogChanged(companyId);
};

export const invalidateCatalogBulk = (options?: BulkOptions): void => {
  const companyId = companyIdOf(options?.where?.companyId);
  if (companyId) onCatalogChanged(companyId);
};

export const invalidateQueuesInstance = (instance?: CompanyCarrier): void => {
  const companyId = companyIdOf(instance?.companyId);
  if (companyId) onQueuesChanged(companyId);
};

export const invalidateQueuesBulk = (options?: BulkOptions): void => {
  const companyId = companyIdOf(options?.where?.companyId);
  if (companyId) onQueuesChanged(companyId);
};
