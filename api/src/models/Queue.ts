import {
  Table,
  Column,
  CreatedAt,
  UpdatedAt,
  Model,
  PrimaryKey,
  AutoIncrement,
  AllowNull,
  Unique,
  BelongsToMany,
  BelongsTo,
  ForeignKey,
  HasMany,
  DataType,
  Default,
  AfterBulkDestroy,
  AfterBulkUpdate,
  AfterCreate,
  AfterDestroy,
  AfterUpdate
} from "sequelize-typescript";
import User from "./User";
import UserQueue from "./UserQueue";
import Company from "./Company";

import Whatsapp from "./Whatsapp";
import WhatsappQueue from "./WhatsappQueue";
import QueueOption from "./QueueOption";
import Prompt from "./Prompt";
import QueueIntegrations from "./QueueIntegrations";
import {
  invalidateQueuesBulk,
  invalidateQueuesInstance
} from "../helpers/companyCacheHooks";

@Table
class Queue extends Model<Queue> {
  @PrimaryKey
  @AutoIncrement
  @Column
  id: number;

  @AllowNull(false)
  @Column
  name: string;

  @AllowNull(false)
  @Column
  color: string;

  @Default("")
  @Column
  greetingMessage: string;

  @Default("")
  @Column
  outOfHoursMessage: string;

  @Column({
    type: DataType.JSONB
  })
  schedules: [];

  @CreatedAt
  createdAt: Date;

  @UpdatedAt
  updatedAt: Date;

  @ForeignKey(() => Company)
  @Column
  companyId: number;

  @BelongsTo(() => Company)
  company: Company;

  @BelongsToMany(() => Whatsapp, () => WhatsappQueue)
  whatsapps: Array<Whatsapp & { WhatsappQueue: WhatsappQueue }>;

  @BelongsToMany(() => User, () => UserQueue)
  users: Array<User & { UserQueue: UserQueue }>;

  @HasMany(() => QueueOption, {
    onDelete: "DELETE",
    onUpdate: "DELETE",
    hooks: true
  })
  options: QueueOption[];

  @Column
  orderQueue: number;

  
  @ForeignKey(() => QueueIntegrations)
  @Column
  integrationId: number;

  @BelongsTo(() => QueueIntegrations)
  queueIntegrations: QueueIntegrations;

  @ForeignKey(() => Prompt)
  @Column
  promptId: number;

  @BelongsTo(() => Prompt)
  prompt: Prompt;

  @AfterCreate
  @AfterUpdate
  @AfterDestroy
  static invalidateQueues(instance: Queue): void {
    invalidateQueuesInstance(instance);
  }

  @AfterBulkUpdate
  @AfterBulkDestroy
  static invalidateQueuesBulk(options: { where?: { companyId?: unknown } }): void {
    invalidateQueuesBulk(options);
  }
}

export default Queue;
