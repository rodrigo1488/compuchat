import Product from "../../models/Product";
import { Op } from "sequelize";
import { productDetailInclude } from "./SyncProductComboItems";
import { appCache, CACHE_TTL } from "../../libs/appCache";

const productInclude = productDetailInclude;

interface Request {
  companyId: number;
  searchParam?: string;
  pageNumber?: number;
  isMenuProduct?: boolean;
  grupo?: string;
}

interface Response {
  products: Product[];
  count: number;
  hasMore: boolean;
  groups?: string[];
}

const ListProductsService = async ({
  companyId,
  searchParam,
  pageNumber = 1,
  isMenuProduct,
  grupo,
}: Request): Promise<Response> => {
  const cacheKey = appCache.buildKey("catalog", companyId, "products", {
    searchParam: searchParam || "",
    pageNumber,
    isMenuProduct: isMenuProduct ?? null,
    grupo: grupo || ""
  });

  const { value } = await appCache.getOrSet(
    cacheKey,
    CACHE_TTL.list,
    () =>
      listProductsFromDb({
        companyId,
        searchParam,
        pageNumber,
        isMenuProduct,
        grupo
      }),
    "catalog"
  );

  return value;
};

const listProductsFromDb = async ({
  companyId,
  searchParam,
  pageNumber = 1,
  isMenuProduct,
  grupo,
}: Request): Promise<Response> => {
  const whereCondition: any = { companyId };

  if (searchParam) {
    whereCondition[Op.or] = [
      { name: { [Op.iLike]: `%${searchParam}%` } },
      { description: { [Op.iLike]: `%${searchParam}%` } },
      { grupo: { [Op.iLike]: `%${searchParam}%` } },
    ];
  }

  if (isMenuProduct !== undefined) {
    whereCondition.isMenuProduct = isMenuProduct;
  }

  if (grupo !== undefined && grupo !== null && grupo !== "") {
    whereCondition.grupo = grupo;
  }

  const limit = 20;
  const offset = (pageNumber - 1) * limit;

  const { count, rows: products } = await Product.findAndCountAll({
    where: whereCondition,
    limit,
    offset,
    order: [["createdAt", "DESC"]],
    include: productInclude,
  });

  const groupsResult = await Product.findAll({
    where: {
      companyId,
      grupo: { [Op.and]: [{ [Op.ne]: null }, { [Op.ne]: "" }] }
    },
    attributes: ["grupo"],
    group: ["grupo"],
    raw: true,
  });
  const groups = groupsResult
    .map((row) => row.grupo)
    .filter((g) => g != null && String(g).trim() !== "")
    .map((g) => String(g).trim())
    .sort();

  return {
    products,
    count,
    hasMore: count > offset + limit,
    groups,
  };
};

export default ListProductsService;
