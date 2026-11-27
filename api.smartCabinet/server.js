const express = require("express");
require("dotenv").config();
const cors = require("cors");
const cookieParser = require("cookie-parser");
const app = express();
const port = process.env.PORT || 4000;
const BASEPATH = process.env.BASEPATH || "api.smartcabinet";
// Hostname được phép gọi API từ trình duyệt (CORS). Pi gọi bằng requests không gửi Origin nên không bị ảnh hưởng.
const allowedHosts = (process.env.ALLOWED_HOSTS || "localhost,127.0.0.1,10.0.108.10")
  .split(",")
  .map((h) => h.trim())
  .filter(Boolean);

const {
  getEquipment_table,
  getEquipment_lending,
  updateEquipment_Lending_Status,
  updateEquipment_Return_Status,
  EquipmentCount,
  CabinetCount,
} = require("./src/routes/equipment");
const { getUser, insertNewCommer, closeDoor } = require("./src/routes/getUser");
const {
  insertLendingHistory,
  borrowEquipment,
  updateStatusReturned,
} = require("./src/routes/lendingHistory");

app.use(
  cors({
    origin: (origin, callback) => {
      if (!origin) return callback(null, true);

      try {
        const { hostname } = new URL(origin);

        if (allowedHosts.includes(hostname)) {
          return callback(null, true);
        }
      } catch (err) {
        return callback(new Error("Invalid Origin"));
      }

      return callback(new Error("Not allowed by CORS"));
    },

    credentials: true,
    methods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allowedHeaders: [
      "Content-Type",
      "Authorization",
      "X-Requested-With",
      "Accept",
    ],
  }),
);

app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));
app.use(cookieParser());

app.get(`/${BASEPATH}/health`, (req, res) => {
  res.json({
    message: "Copyright belongs to Nguyen Viet Anh-175885",
    status: "success",
  });
});
app.get(`/${BASEPATH}/apiGet/getUser`, getUser);
app.post(`/${BASEPATH}/apiPost/insertNewCommer`, insertNewCommer);
app.get(`/${BASEPATH}/apiGet/closeDoor`, closeDoor);
app.get(`/${BASEPATH}/apiGet/getEquipment_table`, getEquipment_table);
app.get(`/${BASEPATH}/apiGet/getEquipmentCount`, EquipmentCount);
app.get(`/${BASEPATH}/apiGet/getEquipment_lending`, getEquipment_lending);
app.get(`/${BASEPATH}/apiGet/CabinetCount`, CabinetCount);
app.put(
  `/${BASEPATH}/apiPut/updateEquipment_Lending_Status`,
  updateEquipment_Lending_Status,
);
app.put(
  `/${BASEPATH}/apiPut/updateEquipment_Return_Status`,
  updateEquipment_Return_Status,
);
app.post(`/${BASEPATH}/apiPost/insertLendingHistory`, insertLendingHistory);
// Mượn = đổi trạng thái + ghi lịch sử trong 1 transaction (Pi gọi endpoint này trước)
app.post(`/${BASEPATH}/apiPost/borrowEquipment`, borrowEquipment);

app.put(`/${BASEPATH}/apiPut/updateStatusReturned`, updateStatusReturned);
app.listen(port, () => {
  console.log(`Server đang chạy tại: http://localhost:${port}`);
});
