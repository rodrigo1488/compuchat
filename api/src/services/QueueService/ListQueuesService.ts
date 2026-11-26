import Queue from "../../models/Queue";
import { appCache, CACHE_TTL } from "../../libs/appCache";

interface Request {
  companyId: number;
}

const ListQueuesService = async ({ companyId }: Request): Promise<Queue[]> => {
  const cacheKey = appCache.buildKey("queues", companyId, "list");
  const { value } = await appCache.getOrSet(
    cacheKey,
    CACHE_TTL.settings,
    async () => {
      const queues = await Queue.findAll({
        where: { companyId },
        order: [["orderQueue", "ASC"]]
      });
      return queues.map(queue => queue.toJSON());
    },
    "queues"
  );

  return value as Queue[];
};

export default ListQueuesService;
