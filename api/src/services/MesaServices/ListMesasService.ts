import Mesa from "../../models/Mesa";
import { Op } from "sequelize";
import { appCache, CACHE_TTL } from "../../libs/appCache";

interface Request {
  companyId: number;
  status?: string;
  type?: string;
  formId?: number;
  section?: string;
}

const ListMesasService = async ({
  companyId,
  status,
  type,
  formId,
  section,
}: Request): Promise<Mesa[]> => {
  const cacheKey = appCache.buildKey("catalog", companyId, "mesas", {
    status: status || "",
    type: type || "",
    formId: formId || null,
    section: section || ""
  });

  const { value } = await appCache.getOrSet(
    cacheKey,
    CACHE_TTL.live,
    () => listMesasFromDb({ companyId, status, type, formId, section }),
    "catalog"
  );

  return value as Mesa[];
};

const listMesasFromDb = async ({
  companyId,
  status,
  type,
  formId,
  section,
}: Request): Promise<Mesa[]> => {
  const whereCondition: any = { companyId };

  if (status) {
    whereCondition.status = status;
  }

  if (type === "mesa" || type === "comanda") {
    whereCondition.type = type;
  }

  if (formId) {
    whereCondition[Op.or] = [{ formId }, { formId: null }];
  }

  if (section) {
    whereCondition.section = section;
  }

  const mesas = await Mesa.findAll({
    where: whereCondition,
    include: [
      { association: "contact", attributes: ["id", "name", "number"] },
      { association: "ticket", attributes: ["id", "status"] },
      { association: "form", attributes: ["id", "slug", "publicId"], required: false },
    ],
    order: [
      ["displayOrder", "ASC"],
      ["number", "ASC"],
    ],
  });

  return mesas;
};

export default ListMesasService;
