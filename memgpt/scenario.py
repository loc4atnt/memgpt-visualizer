"""Demo scenario (all people and events are fictional).

QUICK_PROMPTS  : one-click prompts for each scene of the 6-minute script.
BURST_MESSAGES : long pre-written messages used by the "Burst" button to fill
                 the context window quickly so the 70% warning and the 100%
                 flush happen on stage.
"""

QUICK_PROMPTS = [
    {
        "scene": "Cảnh 1",
        "label": "Giới thiệu bản thân",
        "text": "Chào bạn! Mình là Minh, đang học cao học ngành Khoa học Máy tính. "
        "Sinh nhật mình là 7/2, mình thích cà phê sữa đá, và đang làm luận văn về agent memory.",
    },
    {
        "scene": "Cảnh 4",
        "label": "Hỏi lại chi tiết đã bị evict",
        "text": "À mà hồi nãy mình có kể chuyến đi Đà Lạt, cái quán cà phê mà mình với Lan ngồi cả buổi chiều tên gì nhỉ?",
    },
    {
        "scene": "Cảnh 5",
        "label": "Cập nhật một fact",
        "text": "Cập nhật nè: mình đổi đề tài luận văn sang RAG cho văn bản pháp luật rồi nhé.",
    },
    {
        "scene": "Cảnh 5",
        "label": "Hỏi agent nhớ gì",
        "text": "Bạn còn nhớ những gì về mình?",
    },
    {
        "scene": "Phụ",
        "label": "Hỏi nội dung paper (archival)",
        "text": "Theo paper MemGPT, ngưỡng cảnh báo memory pressure là bao nhiêu phần trăm context window?",
    },
]

BURST_MESSAGES = [
    "Kể bạn nghe chuyến đi Đà Lạt tháng 3 vừa rồi nhé. Mình đi với Lan – bạn thân của mình từ hồi đại học. "
    "Tụi mình đi xe khách giường nằm từ Sài Gòn lúc 10 giờ tối, sáng sớm tới nơi thì trời lạnh khoảng 14 độ, "
    "sương mù dày đặc. Hai đứa thuê một homestay nhỏ gần hồ Xuân Hương, chủ nhà là một cô rất dễ thương, "
    "sáng nào cũng pha trà atiso mời khách. Ngày đầu tiên tụi mình chỉ đi dạo quanh hồ, ăn bánh căn, "
    "rồi về ngủ bù vì quá mệt.",
    "Ngày thứ hai trời mưa lất phất suốt từ sáng tới trưa nên kế hoạch đi đồi chè bị hoãn. Tụi mình lang thang "
    "tìm chỗ trú mưa, đi lạc qua mấy con hẻm dốc, cuối cùng ghé vào một quán tên là Sương Sớm, nằm trên một con "
    "dốc nhỏ nhìn xuống thung lũng. Quán có lò sưởi bằng củi, nhạc Trịnh mở nhỏ, và món cà phê trứng cực kỳ ngon. "
    "Hai đứa ngồi ở đó nguyên buổi chiều, nói chuyện về công việc, về luận văn, về những dự định sau khi tốt nghiệp.",
    "Tối hôm đó tụi mình đi chợ đêm Đà Lạt. Đông kinh khủng, nhưng vui. Mình mua một cái áo len màu xanh rêu, "
    "Lan thì mua cả túi dâu tây và mứt để làm quà cho gia đình. Tụi mình ăn bánh tráng nướng, uống sữa đậu nành "
    "nóng, rồi đứng xem một nhóm sinh viên hát guitar ở bậc thang gần khu Hòa Bình. Có một bạn hát hay tới mức "
    "cả đám đông vỗ tay rất lâu.",
    "Ngày thứ ba trời nắng đẹp nên tụi mình thuê xe máy chạy lên đồi chè Cầu Đất. Đường đi hơi xa, khoảng một "
    "tiếng rưỡi, nhưng cảnh hai bên rất đẹp với rừng thông và những vườn rau thẳng tắp. Trên đồi chè gió thổi "
    "mạnh, tụi mình chụp được rất nhiều ảnh. Trên đường về còn ghé một trang trại hoa cẩm tú cầu, vé vào cửa "
    "khá rẻ, và mình lỡ mua thêm hai chậu sen đá nhỏ.",
    "Buổi tối cuối cùng tụi mình ăn lẩu gà lá é ở một quán quen mà chủ homestay giới thiệu. Nồi lẩu bốc khói "
    "giữa trời lạnh thật sự là trải nghiệm tuyệt vời. Sau đó hai đứa quay lại hồ Xuân Hương đi bộ thêm một "
    "vòng, nói chuyện tới gần nửa đêm. Lan kể là sắp chuyển công tác ra Hà Nội nên chuyến đi này giống như "
    "một chuyến chia tay nho nhỏ, nghe cũng hơi buồn.",
    "Sáng hôm sau tụi mình ra bến xe sớm để về lại Sài Gòn. Trên xe mình ngồi xem lại ảnh và nghĩ rằng lâu lắm "
    "rồi mới có một chuyến đi thong thả như vậy, không deadline, không email. Về tới nhà mình còn mệt cả ngày "
    "nhưng thấy rất vui. Chắc năm sau tụi mình sẽ hẹn nhau đi lại, lần này có thể là Hội An hoặc Phú Quốc.",
]
