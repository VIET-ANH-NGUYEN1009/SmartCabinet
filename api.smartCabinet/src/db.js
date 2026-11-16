const sql = require("mssql");
const dotenv = require("dotenv");
dotenv.config();
const mssqlConfig = {
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  server: process.env.DB_SERVER,
  database: process.env.DB_DATABASE,
  options: {
    encrypt: true,
    trustServerCertificate: true,
  },
  pool: {
    max: 10,
    min: 0,
    idleTimeoutMillis: 30000,
  },
  connectionTimeout: 30000,
  requestTimeout: 30000,
};
let mssqlPool;

const connectDB = async () => {
  try {
    mssqlPool = await new sql.ConnectionPool(mssqlConfig).connect();
    console.log("✅ Liên kết SQL Server thành công!");
    return { mssqlPool };
  } catch (error) {
    console.error("❌ Lỗi khi kết nối Database:", error);
    process.exit(1);
  }
};
const getPool = async () => {
  if (!mssqlPool) await connectDB();
  return mssqlPool;
};
module.exports = { connectDB, getPool };
